"""Retargeting nudges: the rules, the master switch, and every message sent.

A campaign is one rule - "a cart left for an hour", "no order for 14 days" -
with its audience, message, timing and limits held as validated JSON
(services/nudges/config.py), so a new option never needs a migration.

Every send is a row in `nudge_sends`. That row is what stops a customer being
sent the same nudge twice (see `claim_key`), what the frequency caps count,
and what the conversion report reads.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
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

    def __repr__(self) -> str:  # pragma: no cover
        return f"<NudgeSend {self.kind} {self.status} {self.dedupe_key}>"
