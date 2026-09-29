"""Orders.

Money is stored as Numeric(10, 2), never float: the totals here are what the
customer is charged in Stripe and what is reported into Zoho, so binary
rounding error is not acceptable.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey
from app.db.models.enums import OrderStage, PaymentStatus

ZERO = Decimal("0.00")


class Order(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "orders"

    # Short human-readable id shown in WhatsApp templates and Stripe metadata.
    order_number: Mapped[str] = mapped_column(
        String(24), unique=True, index=True, nullable=False
    )

    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    outlet_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outlets.id", ondelete="SET NULL"), index=True
    )
    slot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("delivery_slots.id", ondelete="SET NULL")
    )

    # Cart snapshot: [{retailer_id, name, quantity, unit_price, line_total}].
    # Snapshotted rather than referenced so a later catalogue price change
    # cannot alter an order the customer already paid for.
    items: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    dish_total: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=ZERO, nullable=False)
    delivery_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=ZERO, nullable=False)
    # Uber's "extra taxes/fees" line, kept separate from our own sales tax.
    extra_fees: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=ZERO, nullable=False)
    tax: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=ZERO, nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=ZERO, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)

    # Delivery address snapshot (spec step 12).
    delivery_address: Mapped[str | None] = mapped_column(String(255))
    apartment_unit: Mapped[str | None] = mapped_column(String(120))
    delivery_instructions: Mapped[str | None] = mapped_column(String(500))
    contact_number: Mapped[str | None] = mapped_column(String(32))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    delivery_latitude: Mapped[float | None] = mapped_column(Numeric(9, 6))
    delivery_longitude: Mapped[float | None] = mapped_column(Numeric(9, 6))
    distance_km: Mapped[float | None] = mapped_column(Numeric(6, 2))

    # Slot window copied onto the order so templates and Zoho do not need a join.
    slot_starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    slot_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    slot_label: Mapped[str | None] = mapped_column(String(80))

    # --- Payment -------------------------------------------------------------
    payment_status: Mapped[str] = mapped_column(
        String(20), default=PaymentStatus.NOT_STARTED, nullable=False, index=True
    )
    stripe_session_id: Mapped[str | None] = mapped_column(String(255), index=True)
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(255), index=True)
    stripe_refund_id: Mapped[str | None] = mapped_column(String(255))
    checkout_url: Mapped[str | None] = mapped_column(Text)
    payment_link_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refund_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))

    # --- Fulfilment ----------------------------------------------------------
    stage: Mapped[str] = mapped_column(
        String(32), default=OrderStage.PENDING_PAYMENT, nullable=False, index=True
    )
    stage_timestamps: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_to_kitchen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    out_for_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- External references -------------------------------------------------
    zoho_order_id: Mapped[str | None] = mapped_column(String(40), index=True)
    # Zoho Order Item record per cart line: {retailer_id: zoho id}. Lets a
    # re-push update the lines instead of filing them twice.
    zoho_item_ids: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    uber_quote_id: Mapped[str | None] = mapped_column(String(128))
    uber_quote_raw: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # --- Feedback (spec step 19) ---------------------------------------------
    feedback_rating: Mapped[str | None] = mapped_column(String(20))
    feedback_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    feedback_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    item_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Order {self.order_number} {self.stage} {self.payment_status}>"

    @property
    def is_paid(self) -> bool:
        return self.payment_status == PaymentStatus.PAID

    @property
    def amount_minor_units(self) -> int:
        """Total in the smallest currency unit, as Stripe expects."""
        return int((self.total * 100).to_integral_value())
