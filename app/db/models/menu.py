"""Menu: cuisines, categories and items.

The menu is now owned by this service and managed through the admin dashboard,
imported from the client's Google Sheet. It replaces the Meta catalogue as the
source of truth for what is on sale and what it costs.

Shape mirrors the sheet: one tab per cuisine, section headers as categories,
rows as items.

    Cuisine (Chettinad)
      └── Category (Sambar)
            └── MenuItem (Murungaikai Sambar, $16.53)

Two prices are carried per item. `price` is what the customer pays (the
sheet's MRP); `cost_price` is the sheet's PPP, kept for margin reporting and
never shown to a customer or sent to Stripe.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, Timestamps, UUIDPrimaryKey

ZERO = Decimal("0.00")


class Cuisine(Base, UUIDPrimaryKey, Timestamps):
    """A menu tab: Chettinad, Kerala, Andhra."""

    __tablename__ = "cuisines"

    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    # URL/reply-id safe form, e.g. "chettinad".
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    # Order shown in the WhatsApp cuisine list.
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    categories: Mapped[list["Category"]] = relationship(
        back_populates="cuisine", cascade="all, delete-orphan",
        order_by="Category.position",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Cuisine {self.slug}>"


class Category(Base, UUIDPrimaryKey, Timestamps):
    """A section within a cuisine: Sambar, Rasam, Poriyal."""

    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint("cuisine_id", "slug", name="uq_category_cuisine_slug"),
    )

    cuisine_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cuisines.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    cuisine: Mapped[Cuisine] = relationship(back_populates="categories")
    items: Mapped[list["MenuItem"]] = relationship(
        back_populates="category", cascade="all, delete-orphan",
        order_by="MenuItem.position",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Category {self.slug}>"


class MenuItem(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "menu_items"
    __table_args__ = (
        Index("ix_menu_item_category_position", "category_id", "position"),
    )

    category_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("categories.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Stable id used in cart lines, Stripe metadata and (if synced) the Meta
    # catalogue. Derived from cuisine + category + name so a re-import of the
    # same sheet updates rows rather than duplicating them.
    retailer_id: Mapped[str] = mapped_column(
        String(120), unique=True, index=True, nullable=False
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)

    # What the customer pays (sheet MRP).
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=ZERO, nullable=False)
    # Internal cost (sheet PPP). Never shown to a customer.
    cost_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)

    # Pack details parsed out of the description where present.
    pack_size: Mapped[str | None] = mapped_column(String(80))
    serves: Mapped[str | None] = mapped_column(String(80))

    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # The Zoho Product this dish is, once synced (found again by Product Code
    # = retailer_id if this is lost). See services/catalogue_sync.py.
    zoho_product_id: Mapped[str | None] = mapped_column(String(40), index=True)

    category: Mapped[Category] = relationship(back_populates="items")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MenuItem {self.retailer_id} {self.price}>"

    @property
    def price_display(self) -> str:
        symbol = {"USD": "$", "INR": "₹", "EUR": "€", "GBP": "£"}.get(
            self.currency.upper(), ""
        )
        return f"{symbol}{self.price:.2f}"

    @property
    def margin(self) -> Decimal | None:
        """Price minus cost, when a cost is known."""
        if self.cost_price is None:
            return None
        return (self.price - self.cost_price).quantize(Decimal("0.01"))

    @property
    def margin_percent(self) -> Decimal | None:
        if not self.price or self.cost_price is None:
            return None
        return ((self.price - self.cost_price) / self.price * 100).quantize(
            Decimal("0.1")
        )
