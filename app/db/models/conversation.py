"""Per-customer conversation state.

WhatsApp is stateless between messages, so the current step and everything
gathered so far (cart, chosen outlet, offered slots) lives here. One active
row per customer.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey
from app.db.models.enums import ConversationStep


class Conversation(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("customer_id", name="uq_conversation_customer"),)

    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step: Mapped[str] = mapped_column(
        String(48), default=ConversationStep.START, nullable=False
    )
    previous_step: Mapped[str | None] = mapped_column(String(48))

    # Working scratchpad for the flow: cart lines, offered outlets, offered
    # slots, the draft order id. Kept as JSON because its shape varies by step.
    context: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Conversation customer={self.customer_id} step={self.step}>"

    # -- context helpers ------------------------------------------------------
    def get(self, key: str, default=None):
        return (self.context or {}).get(key, default)

    def set(self, **values) -> None:
        """Merge values into the context.

        Reassigns the dict rather than mutating in place so SQLAlchemy marks
        the JSONB column dirty and actually persists the change.
        """
        self.context = {**(self.context or {}), **values}

    def clear(self, *keys: str) -> None:
        ctx = dict(self.context or {})
        for key in keys:
            ctx.pop(key, None)
        self.context = ctx


class InboundMessage(Base, UUIDPrimaryKey, Timestamps):
    """Dedupe log for Gallabox webhooks.

    Gallabox retries on non-2xx, so the provider message id is stored with a
    unique constraint and a replay is dropped instead of re-running the step.
    """

    __tablename__ = "inbound_messages"

    provider_message_id: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )
    whatsapp_number: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
