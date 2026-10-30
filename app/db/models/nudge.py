"""Retargeting nudges: the rules, the master switch, and every message sent.

A campaign is one rule - "a cart left for an hour", "no order for 14 days" -
with its audience, message, timing and limits held as validated JSON
(services/nudges/config.py), so a new option never needs a migration.

Every send is a row in `nudge_sends`. That row is what stops a customer being
sent the same nudge twice (see `claim_key`), what the frequency caps count,
and what the conversion report reads.

A follow-up journey (services/nudges/followup.py) is a campaign that writes
to the same customer again and again over months, each time with a message
picked from the library (`nudge_messages`). Where each customer is in it -
their reply group, attempts, the next date - is a row in `nudge_journeys`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey


class NudgeKind(StrEnum):
    """What starts a nudge. Values are persisted."""

    ABANDONED_CART = "abandoned_cart"          # dishes in the cart, no order placed
    ABANDONED_CHECKOUT = "abandoned_checkout"  # order placed, payment never made
    REPEAT_PURCHASE = "repeat_purchase"        # paid before, nothing for N days
    NEVER_ORDERED = "never_ordered"            # signed up, never paid
    TARGETED_OFFER = "targeted_offer"          # anyone matching the audience
    FOLLOW_UP = "follow_up"                    # never paid: messages from the library, for months


class JourneyBucket(StrEnum):
    """A customer's follow-up group, set by how they answered. Values are persisted."""

    NO_RESPONSE = "no_response"   # the start: no answer yet
    LATER = "later"               # replied "later"
    NO = "no"                     # declined this offer (still opted in)


class JourneyStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"             # by the team, on the customer lookup
    CONVERTED = "converted"       # paid for an order: the journey's job is done
    ENDED = "ended"               # ran its length or its attempts ("dead lead")


class NudgeStatus(StrEnum):
    SENDING = "sending"    # claimed; the message is on its way
    SENT = "sent"
    FAILED = "failed"
    DRY_RUN = "dry_run"    # would have been sent; dry-run mode sent nothing
    TEST = "test"          # a test send to a chosen number


class NudgeSettings(Base, UUIDPrimaryKey, Timestamps):
    """The one row of global settings. `enabled` is the master switch: off,
    and nothing is sent by the scheduler or by a manual run."""

    __tablename__ = "nudge_settings"

    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    values: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String(120))


class NudgeCampaign(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "nudge_campaigns"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # A new campaign starts paused, so saving one never messages anybody.
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Higher goes first when one customer qualifies for several in a run.
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # The coupon the message offers, if any (services/promotions.py).
    promotion_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("promotions.id", ondelete="SET NULL"), index=True)
    # trigger / audience / message / schedule / limits / offer / zoho.
    config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(120))
    updated_by: Mapped[str | None] = mapped_column(String(120))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<NudgeCampaign {self.name} {self.kind} active={self.is_active}>"


class NudgeSend(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "nudge_sends"

    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("nudge_campaigns.id", ondelete="SET NULL"), index=True)
    campaign_name: Mapped[str | None] = mapped_column(String(120))
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    whatsapp_number: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # What this nudge is about: one cart, one unpaid order, one "since your
    # last order". A trigger is nudged once, however many runs see it.
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False)
    # Unique while a send is claimed or done, so two workers cannot both send
    # it. Cleared on failure, so the next run may try again.
    claim_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    channel: Mapped[str | None] = mapped_column(String(16))   # text | button | template
    message: Mapped[str | None] = mapped_column(Text)
    promo_code: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(String(500))
    zoho_synced: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    zoho_error: Mapped[str | None] = mapped_column(String(500))
    actor: Mapped[str | None] = mapped_column(String(120))     # scheduler, or who ran it
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # A follow-up's library message (its code, e.g. "M2-03") and why it was
    # picked, for the per-message results.
    message_code: Mapped[str | None] = mapped_column(String(40), index=True)
    note: Mapped[str | None] = mapped_column(String(200))
    # Gallabox's id for the message, and its delivery status when Gallabox
    # reports one (delivered, read, failed).
    provider_message_id: Mapped[str | None] = mapped_column(String(120), index=True)
    delivery_status: Mapped[str | None] = mapped_column(String(20))
    delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the customer answered it with an ORDER FOOD keyword.
    keyword_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<NudgeSend {self.kind} {self.status} {self.dedupe_key}>"


class NudgeMessage(Base, UUIDPrimaryKey, Timestamps):
    """One message in the library a follow-up journey picks from.

    `month` is the stage of the journey it belongs to (month 1, 2...); empty
    fits any month. `starts_on`/`ends_on` make it an occasion message, only
    sent between those dates and preferred over the month's own then.
    """

    __tablename__ = "nudge_messages"

    # Stable, from the content plan ("M1-01"); imports match on it.
    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    # Which journey's messages these are; a campaign names its pool.
    pool: Mapped[str] = mapped_column(String(60), default="SMO", nullable=False, index=True)
    month: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    angle: Mapped[str | None] = mapped_column(String(80))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Reply groups it suits (JourneyBucket values); empty is all of them.
    buckets: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    occasion: Mapped[str | None] = mapped_column(String(80))
    starts_on: Mapped[date | None] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    # Sent only while one of these dishes is on the menu.
    dishes: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # An occasion's service area (kitchen codes; empty is every kitchen) and
    # its ordering cutoff: not sent once that day has passed.
    kitchens: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    order_by: Mapped[date | None] = mapped_column(Date)
    # The approved WhatsApp template for customers outside the 24-hour window.
    wa_template: Mapped[str | None] = mapped_column(String(120))
    wa_params: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    wa_button: Mapped[str] = mapped_column(String(16), default="none", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(120))
    updated_by: Mapped[str | None] = mapped_column(String(120))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<NudgeMessage {self.code} month={self.month} active={self.is_active}>"


class NudgeJourney(Base, UUIDPrimaryKey, Timestamps):
    """One customer's place in one follow-up campaign. Made by its first send."""

    __tablename__ = "nudge_journeys"
    __table_args__ = (UniqueConstraint("campaign_id", "customer_id",
                                       name="uq_nudge_journeys_campaign_customer"),)

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("nudge_campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    bucket: Mapped[str] = mapped_column(String(16), default=JourneyBucket.NO_RESPONSE,
                                        nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=JourneyStatus.ACTIVE,
                                        nullable=False, index=True)
    # Bumped by a restart, so a restarted journey's sends never collide with
    # the earlier ones' (nudge_sends.dedupe_key).
    round: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bucket_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Failed sends of the attempt now due; three count as the attempt.
    failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                  index=True)
    last_message_code: Mapped[str | None] = mapped_column(String(40))
    # The latest messages sent, newest last: [{"code": "M1-03", "at": "..."}].
    recent: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    last_reply: Mapped[str | None] = mapped_column(String(16))
    last_reply_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None] = mapped_column(String(40))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The ordering flow ORDER FOOD starts (FlowStatus): follow-ups wait while
    # it is active, and resume if it is abandoned without an order.
    flow_status: Mapped[str] = mapped_column(String(16), default="not_started", nullable=False)
    flow_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_keyword: Mapped[str | None] = mapped_column(String(60))
    # The customer's time zone used for sending, and where it came from:
    # "kitchen" (their kitchen's) or "fallback" (the nudge settings').
    timezone: Mapped[str | None] = mapped_column(String(64))
    timezone_source: Mapped[str | None] = mapped_column(String(16))
    # Why a due follow-up was last held back, and when.
    last_skip_reason: Mapped[str | None] = mapped_column(String(60))
    last_skip_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The Zoho record's retargeting fields, last written (or why not).
    zoho_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    zoho_error: Mapped[str | None] = mapped_column(String(300))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<NudgeJourney {self.bucket} {self.status} attempts={self.attempts}>"


class FlowStatus(StrEnum):
    """The ordering flow a follow-up customer started (SMO_Flow_Status)."""

    NOT_STARTED = "not_started"
    ACTIVE = "active"             # sent ORDER FOOD: follow-ups wait
    COMPLETED = "completed"       # paid for an order
    ABANDONED = "abandoned"       # no order in time: follow-ups resume


class NudgeSkip(Base, UUIDPrimaryKey, Timestamps):
    """A due follow-up held back, or an occasion message passed over, and why.

    One row per customer, day, reason and message, counted up, so a customer
    held back every five minutes is one row a day, not three hundred.
    """

    __tablename__ = "nudge_skips"
    __table_args__ = (UniqueConstraint("campaign_id", "customer_id", "day", "reason",
                                       "message_code", name="uq_nudge_skips_day"),)

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("nudge_campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    day: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(60), nullable=False)
    # The message passed over ("" when none was chosen yet).
    message_code: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
