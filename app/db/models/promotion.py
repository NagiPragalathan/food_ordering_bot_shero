"""Promotions: coupon codes a customer applies at checkout.

Managed on the admin Offers page (admin/routes/promotions.py). The rules a
code is checked against, and the discount it gives, are in
services/promotions.py; pricing.price_cart applies it, and the order keeps a
snapshot of the code, its title and the amount taken off, so the bill, Stripe
and Zoho show what was actually charged even if the promotion is later edited
or deleted.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Boolean, Date, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey


class DiscountType(StrEnum):
    PERCENT = "percent"              # % off the dishes, optionally capped
    FLAT = "flat"                    # a fixed amount off the dishes
    FREE_DELIVERY = "free_delivery"  # the delivery charge, waived


class Placement(StrEnum):
    """Where a customer sees the offer. A hidden one still works when typed."""

    MENU = "menu"            # banner on the menu page, and listed at checkout
    CHECKOUT = "checkout"    # listed at checkout only
    HIDDEN = "hidden"        # never listed: for codes handed out privately


class Promotion(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "promotions"

    title: Mapped[str] = mapped_column(String(120), nullable=False)
    # Stored upper-case; customers may type it in any case.
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    # Customer-facing copy: the banner label ("FLAT 20% OFF") and one line
    # explaining the offer.
    tag: Mapped[str | None] = mapped_column(String(40))
    description: Mapped[str | None] = mapped_column(String(255))

    discount_type: Mapped[str] = mapped_column(
        String(20), default=DiscountType.PERCENT, nullable=False)
    # Percent (0-100) or an amount, by type. Unused for free delivery.
    discount_value: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=Decimal("0"), nullable=False)
    # Dish subtotal needed before the code applies; 0 for none.
    min_order: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=Decimal("0"), nullable=False)
    # Cap on a percentage discount; None for no cap.
    max_discount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))

    # Redemptions across everyone, and per customer; None for unlimited. A
    # paid order counts, and so does an unpaid one whose payment link is
    # still open, so the last use cannot be handed out twice.
    usage_limit: Mapped[int | None] = mapped_column(Integer)
    per_customer_limit: Mapped[int | None] = mapped_column(Integer)
    # Only for a customer with no paid order yet.
    first_order_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Only for orders cooked by this kitchen; None for every kitchen.
    outlet_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outlets.id", ondelete="SET NULL"))

    # Inclusive dates, in the kitchen's timezone. None leaves that end open.
    starts_on: Mapped[date | None] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)

    placement: Mapped[str] = mapped_column(
        String(20), default=Placement.CHECKOUT, nullable=False)
    # Lower shows first wherever offers are listed.
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(120))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Promotion {self.code} {self.discount_type} {self.discount_value}>"
