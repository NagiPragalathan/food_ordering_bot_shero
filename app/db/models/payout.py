"""Payments made to kitchen partners for their share of orders.

How an order's money is split between its kitchen and Shero is in
services/payouts.py. When the team pays a kitchen, the orders it covers are
linked to one PartnerPayout, so each order is paid once and the payment can
be undone if it was recorded by mistake.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey

ZERO = Decimal("0.00")


class PartnerPayout(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "partner_payouts"

    outlet_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outlets.id", ondelete="SET NULL"), index=True)
    # Copied so the record still reads right if the kitchen is renamed or deleted.
    outlet_name: Mapped[str] = mapped_column(String(120), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=ZERO, nullable=False)
    order_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # The orders' payment dates, first and last.
    first_paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_on: Mapped[date] = mapped_column(Date, nullable=False)
    reference: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[str | None] = mapped_column(String(120))
    # Undone payouts are kept, with who undid them; their orders are due again.
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[str | None] = mapped_column(String(120))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PartnerPayout {self.outlet_name} {self.amount}>"
