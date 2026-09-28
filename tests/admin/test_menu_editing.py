"""Adding, editing and removing menu rows from the dashboard.

Two things here are worth more than the rest. A dish added by hand must get
the same `retailer_id` the sheet importer would give it, or the next import
creates a duplicate instead of updating the row. And an edit must never
rewrite that id, because carts and paid orders already refer to it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Category, Cuisine, MenuItem
from app.services import menu_admin
from app.services.menu_admin import MenuEditError
from app.services.menu_import import build_retailer_id


async def _category(session: AsyncSession, slug: str = "sambar") -> Category:
    """The first category with this slug - Chettinad's, by fixture order."""
    found = (await session.execute(
        select(Category).where(Category.slug == slug)
        .order_by(Category.created_at).limit(1))).scalars().first()
    assert found is not None, f"fixture has no {slug} category"
    return found


# --- parsing what people actually type -----------------------------------------
def test_price_accepts_the_shapes_people_type():
    assert menu_admin.parse_price("16.53") == Decimal("16.53")
    assert menu_admin.parse_price(" $16.53 ") == Decimal("16.53")
    assert menu_admin.parse_price("1,250") == Decimal("1250.00")
    assert menu_admin.parse_price("16") == Decimal("16.00")


def test_a_blank_cost_stays_unknown_rather_than_zero():
    """0.00 would report a 100% margin, which is a lie, not a blank."""
    assert menu_admin.parse_price("", field="Cost", required=False) is None
    assert menu_admin.parse_price("   ", field="Cost", required=False) is None


def test_a_blank_price_is_refused():
    with pytest.raises(MenuEditError, match="Price is required"):
        menu_admin.parse_price("")


def test_nonsense_and_negative_prices_are_refused():
    with pytest.raises(MenuEditError, match="not a valid price"):
        menu_admin.parse_price("free")
    with pytest.raises(MenuEditError, match="cannot be negative"):
        menu_admin.parse_price("-5")
    with pytest.raises(MenuEditError, match="looks wrong"):
        menu_admin.parse_price("999999")


def test_a_name_is_trimmed_and_required():
    assert menu_admin.clean_name("  Drumstick   Sambar ") == "Drumstick Sambar"
    with pytest.raises(MenuEditError, match="required"):
        menu_admin.clean_name("   ")
    with pytest.raises(MenuEditError, match="200 characters"):
        menu_admin.clean_name("x" * 201)


# --- adding a dish ----------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_dish_added_by_hand_gets_the_importers_retailer_id(
        session: AsyncSession, menu):
    """Otherwise the next sheet import duplicates it instead of updating it."""
    category = await _category(session)
    item = await menu_admin.create_item(
        session, category_id=category.id, name="Vendakkai Sambar", price="14.00")

    assert item.retailer_id == build_retailer_id(
        "Chettinad", "Sambar", "Vendakkai Sambar")


@pytest.mark.asyncio
async def test_a_dish_is_added_to_the_end_of_its_category(
        session: AsyncSession, menu):
    category = await _category(session)
    existing = list((await session.execute(
        select(MenuItem).where(MenuItem.category_id == category.id))).scalars())

    item = await menu_admin.create_item(
        session, category_id=category.id, name="Vendakkai Sambar", price="14.00")

    assert item.position > max(i.position for i in existing)


@pytest.mark.asyncio
async def test_optional_fields_are_stored_when_given(session: AsyncSession, menu):
    category = await _category(session)
    item = await menu_admin.create_item(
        session, category_id=category.id, name="Vendakkai Sambar",
        price="14.00", cost_price="9.20", description="16 oz pack. Serves 3.",
        pack_size="16 oz", serves="Serves 3")

    assert item.cost_price == Decimal("9.20")
    assert item.pack_size == "16 oz"
    assert item.margin == Decimal("4.80")


@pytest.mark.asyncio
async def test_adding_the_same_dish_twice_is_refused_with_a_useful_message(
        session: AsyncSession, menu):
    category = await _category(session)
    await menu_admin.create_item(
        session, category_id=category.id, name="Vendakkai Sambar", price="14.00")

    with pytest.raises(MenuEditError, match="already on the menu"):
        await menu_admin.create_item(
            session, category_id=category.id, name="Vendakkai Sambar", price="15.00")


@pytest.mark.asyncio
async def test_a_dish_needs_a_real_category(session: AsyncSession, menu):
    import uuid

    with pytest.raises(MenuEditError, match="Choose a category"):
        await menu_admin.create_item(
            session, category_id=uuid.uuid4(), name="Orphan", price="1.00")


@pytest.mark.asyncio
async def test_a_new_dish_can_be_added_already_hidden(session: AsyncSession, menu):
    category = await _category(session)
    item = await menu_admin.create_item(
        session, category_id=category.id, name="Coming Soon", price="14.00",
        is_available=False)
    assert item.is_available is False


# --- editing --------------------------------------------------------------------
@pytest.mark.asyncio
async def test_renaming_a_dish_keeps_its_retailer_id(session: AsyncSession, menu):
    """Carts, paid orders and Stripe metadata already point at this id."""
    item = menu["chettinad-sambar-drumstick-sambar"]
    original = item.retailer_id

    await menu_admin.update_item(session, item, name="Murungaikai Sambar")

    assert item.name == "Murungaikai Sambar"
    assert item.retailer_id == original


@pytest.mark.asyncio
async def test_fields_not_sent_are_left_alone(session: AsyncSession, menu):
    """The quick row form posts price only; it must not blank the description."""
    item = menu["chettinad-sambar-drumstick-sambar"]
    description = item.description

    await menu_admin.update_item(session, item, price="13.75")

    assert item.price == Decimal("13.75")
    assert item.description == description


@pytest.mark.asyncio
async def test_every_field_can_be_edited(session: AsyncSession, menu):
    item = menu["chettinad-sambar-drumstick-sambar"]

    await menu_admin.update_item(
        session, item, name="Drumstick Sambar XL", price="19.99",
        cost_price="12.00", description="24 oz pack. Serves 5.",
        pack_size="24 oz", serves="Serves 5", is_available=False)

    assert item.name == "Drumstick Sambar XL"
    assert item.price == Decimal("19.99")
    assert item.cost_price == Decimal("12.00")
    assert item.pack_size == "24 oz"
    assert item.is_available is False


@pytest.mark.asyncio
async def test_an_invalid_edit_leaves_the_dish_untouched(session: AsyncSession, menu):
    item = menu["chettinad-sambar-drumstick-sambar"]
    before = item.price

    with pytest.raises(MenuEditError):
        await menu_admin.update_item(session, item, price="banana")

    assert item.price == before


# --- deleting --------------------------------------------------------------------
@pytest.mark.asyncio
async def test_deleting_a_dish_removes_only_that_dish(session: AsyncSession, menu):
    item = menu["chettinad-sambar-drumstick-sambar"]
    category_id = item.category_id

    name = await menu_admin.delete_item(session, item)

    assert name == "Drumstick Sambar"
    left = list((await session.execute(
        select(MenuItem).where(MenuItem.category_id == category_id))).scalars())
    assert [i.name for i in left] == ["Beans Sambar"]


@pytest.mark.asyncio
async def test_deleting_a_category_takes_its_dishes_with_it(
        session: AsyncSession, menu):
    category = await _category(session)
    name, removed = await menu_admin.delete_category(session, category)

    assert name == "Sambar"
    assert removed == 2
    assert (await session.execute(
        select(MenuItem).where(MenuItem.category_id == category.id))
    ).scalars().first() is None


@pytest.mark.asyncio
async def test_deleting_a_cuisine_reports_how_much_it_removed(
        session: AsyncSession, menu):
    cuisine = (await session.execute(
        select(Cuisine).where(Cuisine.slug == "chettinad"))).scalar_one()

    name, removed = await menu_admin.delete_cuisine(session, cuisine)

    assert name == "Chettinad"
    assert removed == 3          # two sambars and one rasam
    assert (await session.execute(
        select(Category).where(Category.cuisine_id == cuisine.id))
    ).scalars().first() is None


@pytest.mark.asyncio
async def test_deleting_one_cuisine_leaves_the_others(session: AsyncSession, menu):
    cuisine = (await session.execute(
        select(Cuisine).where(Cuisine.slug == "chettinad"))).scalar_one()
    await menu_admin.delete_cuisine(session, cuisine)

    kerala = (await session.execute(
        select(MenuItem).where(MenuItem.retailer_id == "kerala-sambar-appam"))
    ).scalar_one_or_none()
    assert kerala is not None


# --- cuisines and categories -------------------------------------------------------
@pytest.mark.asyncio
async def test_a_new_cuisine_gets_a_slug_and_goes_last(session: AsyncSession, menu):
    cuisine = await menu_admin.create_cuisine(session, "  North  Indian ")

    assert cuisine.name == "North Indian"
    assert cuisine.slug == "north-indian"
    assert cuisine.is_active is True
    assert cuisine.position > 0


@pytest.mark.asyncio
async def test_a_duplicate_cuisine_is_refused(session: AsyncSession, menu):
    with pytest.raises(MenuEditError, match="already exists"):
        await menu_admin.create_cuisine(session, "Chettinad")


@pytest.mark.asyncio
async def test_a_category_is_created_inside_its_cuisine(session: AsyncSession, menu):
    cuisine = (await session.execute(
        select(Cuisine).where(Cuisine.slug == "chettinad"))).scalar_one()

    category = await menu_admin.create_category(session, cuisine.id, "Poriyal")

    assert category.cuisine_id == cuisine.id
    assert category.slug == "poriyal"


@pytest.mark.asyncio
async def test_the_same_category_name_is_allowed_in_a_different_cuisine(
        session: AsyncSession, menu):
    """The fixture already has "Sambar" in both Chettinad and Kerala."""
    kerala = (await session.execute(
        select(Cuisine).where(Cuisine.slug == "kerala"))).scalar_one()

    category = await menu_admin.create_category(session, kerala.id, "Rasam")
    assert category.slug == "rasam"


@pytest.mark.asyncio
async def test_a_duplicate_category_in_the_same_cuisine_is_refused(
        session: AsyncSession, menu):
    cuisine = (await session.execute(
        select(Cuisine).where(Cuisine.slug == "chettinad"))).scalar_one()

    with pytest.raises(MenuEditError, match="already a category"):
        await menu_admin.create_category(session, cuisine.id, "Sambar")


# --- photos ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_an_uploaded_photo_is_stored_locally(session: AsyncSession, menu,
                                                   monkeypatch):
    """Stored on our server, never hot-linked - same rule as the importer."""
    monkeypatch.setattr(menu_admin.media, "store_bytes",
                        lambda data, *, key: "/media/abc123.jpg")

    category = await _category(session)
    item = await menu_admin.create_item(
        session, category_id=category.id, name="With Photo", price="10.00",
        upload_name="dish.jpg", upload_bytes=b"not-really-an-image")

    assert item.image_url == "/media/abc123.jpg"


@pytest.mark.asyncio
async def test_an_unreadable_upload_is_reported_not_swallowed(monkeypatch):
    monkeypatch.setattr(menu_admin.media, "store_bytes", lambda data, *, key: None)

    with pytest.raises(MenuEditError, match="could not be read as an image"):
        await menu_admin.resolve_photo(upload_name="broken.jpg",
                                       upload_bytes=b"xx", url=None, key="k")


@pytest.mark.asyncio
async def test_an_oversized_upload_is_refused_before_processing():
    too_big = b"x" * (menu_admin.media.MAX_BYTES + 1)
    with pytest.raises(MenuEditError, match="too large"):
        await menu_admin.resolve_photo(upload_name="huge.jpg", upload_bytes=too_big,
                                       url=None, key="k")


@pytest.mark.asyncio
async def test_a_photo_already_on_this_server_is_not_refetched():
    """Re-fetching a /media path would fail and blank the photo we hold."""
    kept = await menu_admin.resolve_photo(
        upload_name=None, upload_bytes=None, url="/media/abc123.jpg", key="k")
    assert kept == "/media/abc123.jpg"


@pytest.mark.asyncio
async def test_no_photo_given_leaves_the_dish_without_one():
    assert await menu_admin.resolve_photo(
        upload_name=None, upload_bytes=None, url="  ", key="k") is None


@pytest.mark.asyncio
async def test_an_unreachable_photo_link_is_reported(monkeypatch):
    async def fetch_fails(url, *, client=None):
        return None

    monkeypatch.setattr(menu_admin.media, "fetch", fetch_fails)

    with pytest.raises(MenuEditError, match="could not be downloaded"):
        await menu_admin.resolve_photo(upload_name=None, upload_bytes=None,
                                       url="https://example.com/x.jpg", key="k")


@pytest.mark.asyncio
async def test_editing_without_a_new_photo_keeps_the_old_one(
        session: AsyncSession, menu):
    item = menu["chettinad-sambar-drumstick-sambar"]
    item.image_url = "/media/original.jpg"

    await menu_admin.update_item(session, item, price="12.00", image_url="")

    assert item.image_url == "/media/original.jpg"
