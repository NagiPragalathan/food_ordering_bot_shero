"""Reading the menu for the bot.

The menu now lives in this database (managed through the admin dashboard and
imported from the client's sheet), not in the Meta catalogue.

Everything here returns plain dataclasses rather than ORM rows, so a handler
can stash them in the conversation context without dragging a session around.

Navigation exists because WhatsApp caps a list message at 10 rows and the menu
has 287 items: cuisine -> category (paged) -> items.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Category, Cuisine, MenuItem

log = get_logger(__name__)

# One row of a WhatsApp list is reserved for "More" when paging.
PAGE_SIZE = 9


@dataclass(frozen=True)
class CuisineOption:
    slug: str
    name: str
    item_count: int


@dataclass(frozen=True)
class CategoryOption:
    slug: str
    name: str
    item_count: int


@dataclass(frozen=True)
class ItemOption:
    retailer_id: str
    name: str
    price: Decimal
    currency: str
    pack_size: str | None
    serves: str | None

    @property
    def price_display(self) -> str:
        symbol = {"USD": "$", "INR": "₹", "EUR": "€",
                  "GBP": "£"}.get(self.currency.upper(), "")
        return f"{symbol}{self.price:.2f}"

    @property
    def subtitle(self) -> str:
        """Short line under the name in a list row."""
        parts = [self.price_display]
        if self.pack_size:
            parts.append(self.pack_size)
        if self.serves:
            parts.append(self.serves)
        return " · ".join(parts)


async def list_cuisines(session: AsyncSession) -> list[CuisineOption]:
    """Active cuisines that actually have something available to order."""
    rows = await session.execute(
        select(Cuisine, func.count(MenuItem.id))
        .join(Category, Category.cuisine_id == Cuisine.id)
        .join(MenuItem, MenuItem.category_id == Category.id)
        .where(Cuisine.is_active.is_(True),
               Category.is_active.is_(True),
               MenuItem.is_available.is_(True))
        .group_by(Cuisine.id)
        .order_by(Cuisine.position, Cuisine.name)
    )
    return [
        CuisineOption(slug=cuisine.slug, name=cuisine.name, item_count=int(count))
        for cuisine, count in rows.all()
    ]


async def list_categories(session: AsyncSession,
                          cuisine_slug: str) -> list[CategoryOption]:
    """Categories in a cuisine that have available items."""
    rows = await session.execute(
        select(Category, func.count(MenuItem.id))
        .join(Cuisine, Category.cuisine_id == Cuisine.id)
        .join(MenuItem, MenuItem.category_id == Category.id)
        .where(Cuisine.slug == cuisine_slug,
               Cuisine.is_active.is_(True),
               Category.is_active.is_(True),
               MenuItem.is_available.is_(True))
        .group_by(Category.id)
        .order_by(Category.position, Category.name)
    )
    return [
        CategoryOption(slug=category.slug, name=category.name,
                       item_count=int(count))
        for category, count in rows.all()
    ]


async def list_items(session: AsyncSession, cuisine_slug: str,
                     category_slug: str) -> list[ItemOption]:
    rows = await session.execute(
        select(MenuItem)
        .join(Category, MenuItem.category_id == Category.id)
        .join(Cuisine, Category.cuisine_id == Cuisine.id)
        .where(Cuisine.slug == cuisine_slug,
               Category.slug == category_slug,
               MenuItem.is_available.is_(True))
        .order_by(MenuItem.position, MenuItem.name)
    )
    return [_to_option(item) for item in rows.scalars()]


async def get_by_retailer_ids(session: AsyncSession,
                              retailer_ids: list[str]) -> dict[str, ItemOption]:
    """Look up cart lines. Unavailable items are deliberately excluded."""
    if not retailer_ids:
        return {}
    rows = await session.execute(
        select(MenuItem).where(
            MenuItem.retailer_id.in_(retailer_ids),
            MenuItem.is_available.is_(True),
        )
    )
    return {item.retailer_id: _to_option(item) for item in rows.scalars()}


async def names_for(session: AsyncSession,
                     retailer_ids: list[str]) -> dict[str, str]:
    """Names for ids regardless of availability.

    Used only for error messages: telling a customer "Beans Sambar is no
    longer available" beats showing them a slug.
    """
    if not retailer_ids:
        return {}
    rows = await session.execute(
        select(MenuItem.retailer_id, MenuItem.name)
        .where(MenuItem.retailer_id.in_(retailer_ids))
    )
    return {retailer_id: name for retailer_id, name in rows.all()}


async def search_items(session: AsyncSession, term: str, *,
                       cuisine_slug: str | None = None,
                       limit: int = 9) -> list[ItemOption]:
    """Free-text dish search, so a customer can type 'biryani' and find it."""
    cleaned = (term or "").strip().lower()
    if len(cleaned) < 3:
        return []

    query = (
        select(MenuItem)
        .join(Category, MenuItem.category_id == Category.id)
        .join(Cuisine, Category.cuisine_id == Cuisine.id)
        .where(MenuItem.is_available.is_(True),
               Cuisine.is_active.is_(True),
               func.lower(MenuItem.name).like(f"%{cleaned}%"))
    )
    if cuisine_slug:
        query = query.where(Cuisine.slug == cuisine_slug)

    rows = await session.execute(query.order_by(MenuItem.name).limit(limit))
    return [_to_option(item) for item in rows.scalars()]


def paginate(options: list, page: int) -> tuple[list, bool]:
    """Slice a list for one WhatsApp page. Returns (rows, has_more)."""
    page = max(page, 0)
    start = page * PAGE_SIZE
    window = options[start:start + PAGE_SIZE]
    return window, len(options) > start + PAGE_SIZE


def _to_option(item: MenuItem) -> ItemOption:
    return ItemOption(
        retailer_id=item.retailer_id,
        name=item.name,
        price=Decimal(item.price),
        currency=item.currency,
        pack_size=item.pack_size,
        serves=item.serves,
    )
