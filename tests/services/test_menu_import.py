"""Menu import from the client's Google Sheet layout.

The sample rows here are copied from the real sheet, including its quirks:
run-together descriptions, bracketed local names, and a title row whose
cuisine has to be detected.
"""

from decimal import Decimal

from sqlalchemy import func, select

from app.db.models import Category, Cuisine, MenuItem
from app.services.menu_import import (
    build_retailer_id,
    extract_pack_details,
    import_from_csv_text,
    parse_csv,
    sheet_id_from_url,
    slugify,
)

SHEET = """,,,,,,
,Chettinad Cuisine - USA Menu,,,,,
,1,Sambar,Description,IMAGE,PPP ( IN USD ),MRP ( IN USD )
,1,Murungaikai ( Drumstick ) Sambar,"16 oz pack. Serves 3 - 4 x 1 meal.A medium-thin sambar.",,10.78,16.53
,2,Beans Sambar,"16 oz pack. Serves 3 - 4 x 1 meal.",,7.36,11.28
,2,Rasam,Description,IMAGE,PPP ( IN USD ),MRP ( IN USD )
,1,Thakkali Rasam,"16 oz pack. Serves 3 - 4 x 1 meal.",,6.20,9.50
"""


# --- parsing -----------------------------------------------------------------
def test_cuisine_is_detected_from_the_title_row():
    tab = parse_csv(SHEET)
    assert tab.cuisine == "Chettinad"
    assert tab.warnings == []


def test_categories_and_items_are_grouped():
    tab = parse_csv(SHEET)
    assert tab.categories == ["Sambar", "Rasam"]
    assert len(tab.items) == 3
    assert [i.category for i in tab.items] == ["Sambar", "Sambar", "Rasam"]


def test_mrp_is_the_customer_price_and_ppp_is_the_cost():
    first = parse_csv(SHEET).items[0]
    assert first.price == Decimal("16.53")       # MRP
    assert first.cost_price == Decimal("10.78")  # PPP


def test_pack_size_and_serves_are_extracted():
    pack, serves = extract_pack_details("16 oz pack. Serves 3 - 4 x 1 meal.")
    assert pack == "16 oz pack"
    assert serves == "Serves 3 - 4"


def test_run_together_descriptions_get_spaced():
    tab = parse_csv(SHEET)
    assert "meal. A medium-thin" in tab.items[0].description


def test_rows_before_a_category_header_are_reported_not_silently_dropped():
    stray = ",\n,1,Orphan Dish,Some description,,1.00,2.00\n"
    tab = parse_csv(stray, fallback_cuisine="Test")
    assert tab.items == []
    assert any("before any category" in w for w in tab.warnings)


def test_unreadable_price_is_warned_about():
    broken = SHEET + ",3,Mystery Dish,A description,,1.00,not-a-price\n"
    tab = parse_csv(broken)
    # The row has no usable MRP, so it is skipped rather than priced at zero.
    assert all(i.name != "Mystery Dish" for i in tab.items)


def test_missing_title_row_falls_back_to_the_supplied_cuisine():
    no_title = "\n".join(SHEET.splitlines()[2:])
    tab = parse_csv(no_title, fallback_cuisine="Andhra")
    assert tab.cuisine == "Andhra"
    assert len(tab.items) == 3


# --- ids ---------------------------------------------------------------------
def test_slugify_is_url_and_reply_id_safe():
    assert slugify("Murungaikai ( Drumstick ) Sambar") == "murungaikai-drumstick-sambar"
    assert slugify("Batter & Paste") == "batter-paste"
    assert slugify("") == "item"


def test_retailer_id_is_stable_and_bounded():
    first = build_retailer_id("Chettinad", "Sambar", "Drumstick Sambar")
    again = build_retailer_id("Chettinad", "Sambar", "Drumstick Sambar")
    assert first == again == "chettinad-sambar-drumstick-sambar"

    long_id = build_retailer_id("Chettinad", "A" * 60, "B" * 120)
    assert len(long_id) <= 100


def test_retailer_ids_stay_unique_after_truncation():
    a = build_retailer_id("Chettinad", "Category", "X" * 200 + "one")
    b = build_retailer_id("Chettinad", "Category", "X" * 200 + "two")
    assert a != b


def test_sheet_id_is_pulled_out_of_a_full_url():
    url = ("https://docs.google.com/spreadsheets/d/1PZGy9dbgL8HlOAJIPWct"
           "Ose9odMOX_rf_U8ZsjF7CwE/edit?gid=370860987#gid=370860987")
    assert sheet_id_from_url(url) == "1PZGy9dbgL8HlOAJIPWctOse9odMOX_rf_U8ZsjF7CwE"


# --- importing ---------------------------------------------------------------
async def test_import_creates_the_full_tree(session):
    result = await import_from_csv_text(session, SHEET)

    assert result.cuisines == ["Chettinad"]
    assert result.categories_created == 2
    assert result.items_created == 3

    assert await session.scalar(select(func.count(Cuisine.id))) == 1
    assert await session.scalar(select(func.count(Category.id))) == 2
    assert await session.scalar(select(func.count(MenuItem.id))) == 3


async def test_reimport_updates_instead_of_duplicating(session):
    await import_from_csv_text(session, SHEET)
    again = await import_from_csv_text(session, SHEET)

    assert again.items_created == 0
    assert again.items_updated == 3
    assert await session.scalar(select(func.count(MenuItem.id))) == 3


async def test_reimport_picks_up_an_edited_price(session):
    await import_from_csv_text(session, SHEET)
    edited = SHEET.replace("10.78,16.53", "11.00,19.99")
    await import_from_csv_text(session, edited)

    item = await session.scalar(
        select(MenuItem).where(
            MenuItem.retailer_id == "chettinad-sambar-murungaikai-drumstick-sambar")
    )
    assert item.price == Decimal("19.99")
    assert item.cost_price == Decimal("11.00")


async def test_items_dropped_from_the_sheet_are_hidden_not_deleted(session):
    """Order history must keep resolving, so rows are deactivated."""
    await import_from_csv_text(session, SHEET)

    without_beans = "\n".join(
        line for line in SHEET.splitlines() if "Beans Sambar" not in line)
    result = await import_from_csv_text(session, without_beans,
                                        deactivate_missing=True)

    assert result.items_deactivated == 1
    beans = await session.scalar(
        select(MenuItem).where(MenuItem.retailer_id == "chettinad-sambar-beans-sambar")
    )
    assert beans is not None, "the row must survive"
    assert beans.is_available is False


async def test_deactivation_can_be_turned_off(session):
    await import_from_csv_text(session, SHEET)
    without_beans = "\n".join(
        line for line in SHEET.splitlines() if "Beans Sambar" not in line)
    result = await import_from_csv_text(session, without_beans,
                                        deactivate_missing=False)
    assert result.items_deactivated == 0


async def test_two_cuisines_stay_separate(session):
    await import_from_csv_text(session, SHEET)
    kerala = SHEET.replace("Chettinad Cuisine", "Kerala Cuisine")
    await import_from_csv_text(session, kerala)

    assert await session.scalar(select(func.count(Cuisine.id))) == 2
    # Same dish names, but namespaced ids, so nothing collides.
    assert await session.scalar(select(func.count(MenuItem.id))) == 6
