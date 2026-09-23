"""Delivery slots and the holds placed on them while payment is pending."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey
from app.db.models.enums import SlotHoldStatus


def _utc(value: datetime) -> datetime:
    """Treat a naive timestamp as UTC (see slots.as_utc)."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class DeliverySlot(Base, UUIDPrimaryKey, Timestamps):
    """A bookable delivery window for one outlet.

    `capacity` minus active holds/bookings is what the customer is offered, so
    two people paying at once cannot oversell the same window.
    """

    __tablename__ = "delivery_slots"
    __table_args__ = (
        Index("ix_slot_outlet_start", "outlet_id", "starts_at"),
    )

    outlet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("outlets.id", ondelete="CASCADE"), nullable=False
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    capacity: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    # Denormalised counter kept in step with SlotHold rows; cheap to read when
    # listing slots for the customer.
    reserved_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    @property
    def remaining(self) -> int:
        return max(self.capacity - self.reserved_count, 0)

    @property
    def is_available(self) -> bool:
        return self.is_active and self.remaining > 0

    def label(self, tz=None) -> str:
        """Customer-facing label, e.g. 'Mon 22 Sep, 6:00 PM - 7:00 PM'."""
        # Pin naive values to UTC first; astimezone() on a naive datetime
        # would otherwise assume the server's local zone.
        start = _utc(self.starts_at)
        end = _utc(self.ends_at)
        if tz:
            start, end = start.astimezone(tz), end.astimezone(tz)
        return (
            f"{start.strftime('%a %d %b')}, "
            f"{start.strftime('%I:%M %p').lstrip('0')} - "
            f"{end.strftime('%I:%M %p').lstrip('0')}"
        )


class SlotHold(Base, UUIDPrimaryKey, Timestamps):
    """Reservation of a slot against an order.

    Created when the customer picks a slot (spec step 13, "slot is held while
    payment is pending"), promoted to BOOKED by the Stripe webhook (step 16),
    or RELEASED when the payment link expires (step 15).
    """

    __tablename__ = "slot_holds"

    slot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("delivery_slots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default=SlotHoldStatus.HELD, nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
