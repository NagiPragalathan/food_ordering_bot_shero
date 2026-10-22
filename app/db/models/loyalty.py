"""Loyalty wallet and immutable transaction audit trail."""
from __future__ import annotations
import uuid
from sqlalchemy import BigInteger, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base, Timestamps, UUIDPrimaryKey

class LoyaltyAccount(Base, Timestamps):
    __tablename__ = "loyalty_accounts"
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), primary_key=True)
    balance: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    eligible_orders: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

class LoyaltyEntry(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "loyalty_entries"
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True, nullable=False)
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), index=True)
    event_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    points: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str | None] = mapped_column(String(255))
