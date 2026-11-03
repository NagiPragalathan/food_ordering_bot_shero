"""Nudges: where each customer is in the retargeting, and every message sent.

The rules themselves (which stage waits how long, which message, the No /
Later / No response sequences) are one JSON setting (services/nudges/config.py),
so a new option never needs a migration.

`nudge_leads` has one row per customer the nudges have acted on: the stage
being watched, whether its message went, and - after they tapped No or Later,
or did not answer - the bucket they are in and how many messages it has sent.
`nudge_sends` has a row per message, with whether it went and what Gallabox
reported back.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey


class NudgeBucket(StrEnum):
    NO_RESPONSE = "no_response"     # did not answer the stage message in time
    LATER = "later"                 # tapped Later
    NO = "no"                       # tapped No
    DEAD = "dead"                   # the sequence ended, or they replied STOP


class NudgeLead(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "nudge_leads"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, unique=True)
    # The stage being watched, when they entered it, and when its message went
    # (once per stage entry). A move to another stage starts again.
    stage: Mapped[str] = mapped_column(String(40), nullable=False)
    stage_entered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stage_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bucket: Mapped[str | None] = mapped_column(String(16), index=True)
    bucket_from: Mapped[str | None] = mapped_column(String(40))     # the stage they were in
    bucket_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # sent in the bucket
    failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # in a row
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # Why nothing is going right now ("Message not approved yet"), for the page.
    waiting: Mapped[str | None] = mapped_column(String(200))
    last_reply: Mapped[str | None] = mapped_column(String(20))
    last_reply_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    zoho_error: Mapped[str | None] = mapped_column(String(300))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<NudgeLead {self.stage} bucket={self.bucket} attempts={self.attempts}>"


class NudgeSend(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "nudge_sends"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    # "stage" for a stage's message, else the bucket's name.
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(40))
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    template: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)      # sent | failed
    error: Mapped[str | None] = mapped_column(String(500))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    # Gallabox's id for the message, and its delivery status when Gallabox
    # reports one (delivered, read, failed).
    provider_message_id: Mapped[str | None] = mapped_column(String(120), index=True)
    delivery_status: Mapped[str | None] = mapped_column(String(20))
    delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The button they tapped in answer: No, Later or STOP.
    reply: Mapped[str | None] = mapped_column(String(20))
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<NudgeSend {self.kind} {self.template} {self.status}>"
