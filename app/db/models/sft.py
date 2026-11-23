"""Shero Food Training (SFT): the course's WhatsApp leads (app/sft/).

A separate funnel from food ordering: its own leads and their timeline, kept
in Shero US's database beside the ordering tables but never mixed with them.
One lead per WhatsApp number.

    SftLead   where the person is in the course journey (stage), what the
              bot is waiting for from them (step), the follow-ups, the
              payment, a hand-over to the team and the Zoho record
    SftEvent  everything that happened to them, in order: what they said,
              what the bot sent, each stage, payments, hand-overs, the
              team's actions. The admin timeline, the analytics and the
              Zoho notes all read it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey


class SftLead(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "sft_leads"

    whatsapp_number: Mapped[str] = mapped_column(String(20), unique=True, index=True,
                                                 nullable=False)
    name: Mapped[str | None] = mapped_column(String(120))
    # The name on their WhatsApp profile, kept apart from the name they gave.
    profile_name: Mapped[str | None] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(255))
    # "business" (start a food business) or "skills" (improve my skills).
    goal: Mapped[str | None] = mapped_column(String(20))

    # Where they are in the journey (app/sft/content.py STAGES) and what the
    # bot is waiting for from them (STEPS).
    stage: Mapped[str] = mapped_column(String(30), default="new", nullable=False, index=True)
    step: Mapped[str] = mapped_column(String(30), default="idle", nullable=False)
    # When each stage was first reached, {"brochure_sent": iso, ...}: the funnel.
    stage_times: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # Where the lead came from, for the analytics and Zoho's Lead Source.
    source: Mapped[str] = mapped_column(String(60), default="WhatsApp", nullable=False)
    source_detail: Mapped[str | None] = mapped_column(String(200))
    ad_id: Mapped[str | None] = mapped_column(String(64))
    campaign_id: Mapped[str | None] = mapped_column(String(64))
    first_message: Mapped[str | None] = mapped_column(String(300))
    # The Gallabox channel (WhatsApp number) they wrote to; replies go there.
    channel_id: Mapped[str | None] = mapped_column(String(64))

    # WhatsApp's 24-hour window opens on their last message.
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Their last move in the journey (a message, a tap, a payment): the
    # follow-ups and the team's tasks are timed from it.
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    # Automated follow-ups (app/sft/nudges.py).
    followups_sent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # The last automated nudge (follow-up, reminder, check-in): the next
    # follow-up keeps its gap from it.
    nudged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    nurture_sent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Set when they chose "Maybe later": the next check-in date.
    nurture_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    lost_reason: Mapped[str | None] = mapped_column(String(60))

    # With the team: they asked a question, so the bot stays silent until
    # someone on the team answers and hands the chat back.
    with_team: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    handed_over_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    question: Mapped[str | None] = mapped_column(Text)

    # A task for the team (the drop-off table): why, how urgent, since when.
    needs_attention: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False,
                                                  index=True)
    attention_reason: Mapped[str | None] = mapped_column(String(200))
    attention_priority: Mapped[str | None] = mapped_column(String(10))
    attention_stage: Mapped[str | None] = mapped_column(String(30))
    attention_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Each drop-off raises one task per stage, never again for the same one.
    attention_raised: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    # Payment: one stable enrol link (/sft/enrol/<pay_token>) that opens a
    # fresh Stripe Checkout whenever it is tapped.
    pay_token: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    checkout_session_id: Mapped[str | None] = mapped_column(String(255))
    checkout_url: Mapped[str | None] = mapped_column(Text)
    checkout_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    link_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    link_clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payment_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    amount_paid: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    payment_intent_id: Mapped[str | None] = mapped_column(String(255))
    test_payment: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # An offer (app/sft/offers.py): the code they sent, and the price of the
    # checkout that is open (a new price opens a new checkout).
    offer_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    offer_code: Mapped[str | None] = mapped_column(String(32))
    checkout_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))

    # After paying: the course link, then "have you been able to open it?".
    course_link_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    access_check_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    access_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Zoho CRM (app/sft/crm.py): synced by a job, never inside the chat.
    zoho_module: Mapped[str | None] = mapped_column(String(20))
    zoho_id: Mapped[str | None] = mapped_column(String(32))
    # True when the record was the bot's own new SFT lead (not an existing
    # food customer's record, whose Lead Source is left as it is).
    zoho_created: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    zoho_tags: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    crm_dirty: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    crm_next_try_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    crm_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    crm_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    crm_error: Mapped[str | None] = mapped_column(String(500))

    # The team's own notes on the lead (admin page).
    notes: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SftLead ***{self.whatsapp_number[-4:]} {self.stage}/{self.step}>"


class SftOffer(Base, UUIDPrimaryKey, Timestamps):
    """A course offer: a code the lead sends on WhatsApp ("SFT50"), or, with
    no code, a price for everyone it covers (a launch price)."""

    __tablename__ = "sft_offers"

    title: Mapped[str] = mapped_column(String(80), nullable=False)
    # Upper case; empty for an automatic offer.
    code: Mapped[str | None] = mapped_column(String(32), index=True)
    # "percent" off, "amount" off, or a fixed "price".
    kind: Mapped[str] = mapped_column(String(10), default="percent", nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    # Local dates (SFT's timezone), both included; empty = open-ended.
    starts_on: Mapped[date | None] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    # Enrollments it may give; empty = no limit.
    usage_limit: Mapped[int | None] = mapped_column(Integer)
    # Lead sources it is for (["Instagram", "Meta Ad"]); empty = every source.
    sources: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    note: Mapped[str | None] = mapped_column(String(255))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SftOffer {self.code or 'auto'} {self.kind} {self.value}>"


class SftEvent(Base, UUIDPrimaryKey):
    __tablename__ = "sft_events"

    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sft_leads.id", ondelete="CASCADE"), nullable=False, index=True)
    # To the microsecond, so a lead's events keep their order.
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    # inbound, sent, stage, followup, attention, handover, payment, team, crm
    kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    # The lead's stage when it happened.
    stage: Mapped[str | None] = mapped_column(String(30))
    detail: Mapped[str | None] = mapped_column(String(600))
    data: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # Still to go to Zoho as a note (stage changes, hand-overs, payments).
    crm_pending: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SftEvent {self.kind} {self.stage}>"
