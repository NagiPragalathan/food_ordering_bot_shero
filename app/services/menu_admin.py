"""Creating, editing and removing menu rows by hand.

The sheet importer owns the bulk menu; this is for the things a kitchen needs
between imports — a festival special added on the morning, a price corrected,
a dish that is off for good.

Both paths write the same rows, so everything here reuses the importer's
`slugify` and `build_retailer_id`. A dish added here and the same dish added
to the sheet later resolve to one row rather than two.

Validation raises `MenuEditError` with a message meant for the person at the
screen, which the admin routes turn into a flash message.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.logging import get_logger
from app.db.models import Category, Cuisine, MenuItem
from app.services import media
from app.services.menu_import import build_retailer_id, slugify

log = get_logger(__name__)

NAME_MAX = 200
CATEGORY_NAME_MAX = 120
CUISINE_NAME_MAX = 80
MAX_PRICE = Decimal("100000")
# Photos are re-encoded to JPEG on the way in, so the upload only has to be
# something Pillow can open.
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


class MenuEditError(Exception):
    """Something the person at the screen can fix themselves."""


# --- parsing -------------------------------------------------------------------
def clean_name(raw: str | None, *, field: str = "Name", limit: int = NAME_MAX) -> str:
    name = " ".join((raw or "").split())
    if not name:
        raise MenuEditError(f"{field} is required.")
    if len(name) > limit:
        raise MenuEditError(f"{field} must be {limit} characters or fewer.")
    return name


def parse_price(raw: str | None, *, field: str = "Price",
                required: bool = True) -> Decimal | None:
    """Accept what people actually type: "$16.53", " 16.53 ", "16".

    Returns None for a blank optional field, so an unknown cost stays unknown
    rather than becoming a misleading 0.00 that would report a 100% margin.
    """
    text = (raw or "").replace("$", "").replace(",", "").strip()
    if not text:
        if required:
            raise MenuEditError(f"{field} is required.")
        return None

    try:
        value = Decimal(text).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise MenuEditError(f"'{raw}' is not a valid {field.lower()}.") from None

    if value < 0:
        raise MenuEditError(f"{field} cannot be negative.")
    if value > MAX_PRICE:
        raise MenuEditError(f"{field} looks wrong - it is over {MAX_PRICE:,.0f}.")
    return value


def optional_text(raw: str | None, *, limit: int) -> str | None:
    text = " ".join((raw or "").split())
    if not text:
        return None
    return text[:limit]


# --- photos ---------------------------------------------------------------------
async def resolve_photo(*, upload_name: str | None, upload_bytes: bytes | None,
                        url: str | None, key: str) -> str | None:
    """Turn whatever the form supplied into a locally served image path.

    An uploaded file wins over a pasted link. Both are stored on this server
    rather than hot-linked, matching the importer: a customer's page must not
    depend on somebody else's host staying up.

    Returns None when nothing usable was given, which leaves the dish showing
    its drawn placeholder.
    """
    if upload_bytes:
        if len(upload_bytes) > media.MAX_BYTES:
            raise MenuEditError(
                f"That image is too large. Please use one under "
                f"{media.MAX_BYTES // (1024 * 1024)} MB."
            )
        stored = media.store_bytes(upload_bytes, key=key)
        if stored is None:
            raise MenuEditError(
                f"'{upload_name or 'that file'}' could not be read as an image."
            )
        return stored

    source = (url or "").strip()
    if not source:
        return None
    # Already ours: re-fetching a /media path would fail and blank the photo.
    if media.is_ours(source):
        return source

    stored = await media.fetch(media.normalise_source(source))
    if stored is None:
        raise MenuEditError(
            "That image link could not be downloaded. Check it is public, "
            "or upload the file instead."
        )
    return stored


# --- cuisines --------------------------------------------------------------------
async def create_cuisine(session: AsyncSession, name: str) -> Cuisine:
    clean = clean_name(name, field="Cuisine name", limit=CUISINE_NAME_MAX)
    slug = slugify(clean, max_length=40)

    existing = (await session.execute(
        select(Cuisine).where(Cuisine.slug == slug))).scalar_one_or_none()
    if existing is not None:
        raise MenuEditError(f"A cuisine called '{existing.name}' already exists.")

    position = await _next_position(session, select(func.max(Cuisine.position)))
    cuisine = Cuisine(name=clean, slug=slug, position=position, is_active=True)
    session.add(cuisine)
    await session.flush()
    log.info("cuisine_created", slug=slug)
    return cuisine


# --- categories -------------------------------------------------------------------
async def create_category(session: AsyncSession, cuisine_id: uuid.UUID,
                          name: str) -> Category:
    cuisine = await session.get(Cuisine, cuisine_id)
    if cuisine is None:
        raise MenuEditError("That cuisine no longer exists.")

    clean = clean_name(name, field="Category name", limit=CATEGORY_NAME_MAX)
    slug = slugify(clean, max_length=60)

    existing = (await session.execute(
        select(Category).where(Category.cuisine_id == cuisine.id,
                               Category.slug == slug))).scalar_one_or_none()
    if existing is not None:
        raise MenuEditError(
            f"'{existing.name}' is already a category in {cuisine.name}.")

    position = await _next_position(
        session,
        select(func.max(Category.position)).where(Category.cuisine_id == cuisine.id),
    )
    category = Category(cuisine_id=cuisine.id, name=clean, slug=slug,
                        position=position, is_active=True)
    session.add(category)
    await session.flush()
    log.info("category_created", cuisine=cuisine.slug, slug=slug)
    return category


# --- items ----------------------------------------------------------------------
async def create_item(session: AsyncSession, *, category_id: uuid.UUID,
                      name: str, price: str | None, cost_price: str | None = None,
                      description: str | None = None, pack_size: str | None = None,
                      serves: str | None = None, image_url: str | None = None,
                      upload_name: str | None = None,
                      upload_bytes: bytes | None = None,
                      is_available: bool = True) -> MenuItem:
    category = await _category_with_cuisine(session, category_id)
    clean = clean_name(name, field="Dish name")

    retailer_id = build_retailer_id(category.cuisine.name, category.name, clean)
    existing = (await session.execute(
        select(MenuItem).where(MenuItem.retailer_id == retailer_id)
    )).scalar_one_or_none()
    if existing is not None:
        raise MenuEditError(
            f"'{existing.name}' is already on the menu in this category. "
            f"Edit it instead of adding it again."
        )

    photo = await resolve_photo(upload_name=upload_name, upload_bytes=upload_bytes,
                                url=image_url, key=retailer_id)
    position = await _next_position(
        session,
        select(func.max(MenuItem.position)).where(MenuItem.category_id == category.id),
    )

    item = MenuItem(
        category_id=category.id,
        retailer_id=retailer_id,
        name=clean,
        description=optional_text(description, limit=2000),
        image_url=photo,
        price=parse_price(price) or Decimal("0.00"),
        cost_price=parse_price(cost_price, field="Cost", required=False),
        pack_size=optional_text(pack_size, limit=80),
        serves=optional_text(serves, limit=80),
        position=position,
        is_available=is_available,
    )
    session.add(item)
    await session.flush()
    log.info("menu_item_created", retailer_id=retailer_id, price=str(item.price))
    return item


async def update_item(session: AsyncSession, item: MenuItem, *,
                      name: str | None = None, price: str | None = None,
                      cost_price: str | None = None,
                      description: str | None = None,
                      pack_size: str | None = None, serves: str | None = None,
                      image_url: str | None = None,
                      upload_name: str | None = None,
                      upload_bytes: bytes | None = None,
                      is_available: bool | None = None) -> MenuItem:
    """Apply whatever the form sent. Fields left as None are not touched.

    The `retailer_id` is deliberately **not** regenerated when the name
    changes: it is the id carts, past orders and Stripe metadata already use,
    so rewriting it would orphan a cart somebody is holding right now.
    """
    if name is not None:
        item.name = clean_name(name, field="Dish name")
    if price is not None:
        item.price = parse_price(price) or Decimal("0.00")
    if cost_price is not None:
        item.cost_price = parse_price(cost_price, field="Cost", required=False)
    if description is not None:
        item.description = optional_text(description, limit=2000)
    if pack_size is not None:
        item.pack_size = optional_text(pack_size, limit=80)
    if serves is not None:
        item.serves = optional_text(serves, limit=80)
    if is_available is not None:
        item.is_available = is_available

    if upload_bytes or (image_url is not None and image_url.strip()):
        photo = await resolve_photo(upload_name=upload_name,
                                    upload_bytes=upload_bytes,
                                    url=image_url, key=item.retailer_id)
        if photo:
            item.image_url = photo

    await session.flush()
    log.info("menu_item_updated", retailer_id=item.retailer_id,
             price=str(item.price), available=item.is_available)
    return item


async def delete_item(session: AsyncSession, item: MenuItem) -> str:
    """Remove a dish for good. Returns its name, for the confirmation message.

    Safe for order history: an order snapshots its lines as JSON rather than
    pointing at this row, so a delivered order still shows what was in it. A
    cart holding this dish simply finds it gone at checkout and says so, which
    is the same path an out-of-stock dish takes.
    """
    name = item.name
    log.info("menu_item_deleted", retailer_id=item.retailer_id, name=name)
    await session.delete(item)
    await session.flush()
    return name


async def delete_category(session: AsyncSession, category: Category) -> tuple[str, int]:
    """Remove a category and every dish in it. Returns (name, dishes removed)."""
    count = (await session.execute(
        select(func.count()).select_from(MenuItem)
        .where(MenuItem.category_id == category.id)
    )).scalar_one()
    name = category.name
    log.info("category_deleted", slug=category.slug, items=count)
    await session.delete(category)
    await session.flush()
    return name, int(count)


async def delete_cuisine(session: AsyncSession, cuisine: Cuisine) -> tuple[str, int]:
    """Remove a cuisine, its categories and all of their dishes."""
    count = (await session.execute(
        select(func.count()).select_from(MenuItem)
        .join(Category, MenuItem.category_id == Category.id)
        .where(Category.cuisine_id == cuisine.id)
    )).scalar_one()
    name = cuisine.name
    log.info("cuisine_deleted", slug=cuisine.slug, items=count)
    await session.delete(cuisine)
    await session.flush()
    return name, int(count)


# --- helpers ----------------------------------------------------------------------
async def _category_with_cuisine(session: AsyncSession,
                                 category_id: uuid.UUID) -> Category:
    category = (await session.execute(
        select(Category)
        .options(selectinload(Category.cuisine))
        .where(Category.id == category_id)
    )).scalar_one_or_none()
    if category is None:
        raise MenuEditError("Choose a category for the dish.")
    return category


async def _next_position(session: AsyncSession, highest) -> int:
    """One past the current highest, so a new row lands at the end."""
    current = (await session.execute(highest)).scalar_one_or_none()
    return int(current or 0) + 1
