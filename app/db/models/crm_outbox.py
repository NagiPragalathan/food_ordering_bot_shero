"""Events on their way to DST India's own CRM (services/dst_crm.py).

Each row is one thing that happened to a customer - a stage, a nudge, a paid
order, an order's next step - written in the same transaction as the change
itself, then posted to the CRM by the `crm_outbox` job, in order, until the
CRM takes it. So the chat never waits on the CRM, and nothing is lost while
the CRM is down or being redeployed.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey

PENDING, SENT, FAILED = "pending", "sent", "failed"


class CrmOutbox(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "crm_outbox"

    event: Mapped[str] = mapped_column(String(30), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    # One row per thing that can only happen once ("order.paid:DST-..."),
    # so a replayed payment webhook does not send the order twice.
    dedupe_key: Mapped[str | None] = mapped_column(String(160), unique=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # When it was queued, to the microsecond (created_at is to the second on
    # SQLite): each customer's events go in this order.
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(12), default=PENDING, nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_error: Mapped[str | None] = mapped_column(String(500))
    # What the CRM did with it ("lead created; pipeline welcome"), once sent.
    result: Mapped[str | None] = mapped_column(String(500))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CrmOutbox {self.event} {self.status} attempts={self.attempts}>"
