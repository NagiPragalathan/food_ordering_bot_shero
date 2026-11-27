"""Bulk price update (services/price_update.py): PPP, and MRP when asked, of
dishes already on the menu, from a pricing sheet in either business's layout.
It never adds, renames or hides a dish, and never puts a price on a guess."""

from __future__ import annotations

import io
from decimal import Decimal

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app.db.models import MenuItem
from app.services import price_update as pu

# Shero US: # | name | Description | Image | PPP | MRP
US_TAB = [
    ["", "", "", "", "", "", ""],
    ["", "Chettinad Cuisine - New Category  US", "", "", "", "", ""],
    ["", "1", "Sambar", "Description", "IMAGE", "PPP ( IN USD )", "MRP ( IN USD )"],
    ["", "1", "Drumstick Sambar - 16 OZ", "Lentil stew.", "", "9.00", "13.00"],
    ["", "2", "Beans  Sambar", "Beans.", "", "7.36", "11.28"],
    ["", "3", "Brand New Dish - 16 OZ", "New.", "", "4.00", "6.00"],
]
# DST India: # | # | name | PPP | MRP | Description | Image | Video
INDIA_TAB = [
    ["", "", "", "Kerala Cuisine - New Category OPD Wise - After Apr 10", "", "", "", ""],
    ["", "1", "", "DHS / Festival Menu", "PPP", "MRP", "Description", "Image"],
    ["", "", "1", "", "", "", "", ""],
    ["", "2", "10", "Kerala Special", "PPP", "MRP", "Description", "Image"],
    ["", "1", "1", "Appam", "2.50", "3.25", "Rice hoppers", ""],
]


def plan_for(rows):
    plan = pu.PricePlan()
    return plan, pu.read_tabs(rows, plan)


def menu_item(menu, name):
    return next(item for item in menu.values() if item.name == name)


def test_both_layouts_are_read_by_their_labels():
    us = pu.parse_tab(US_TAB, "Chettinad US - Apr'26")
    assert [(r.cuisine, r.category, r.name, r.ppp, r.mrp) for r in us][:2] == [
        ("Chettinad", "Sambar", "Drumstick Sambar - 16 OZ", Decimal("9.00"), Decimal("13.00")),
        ("Chettinad", "Sambar", "Beans Sambar", Decimal("7.36"), Decimal("11.28"))]
    india = pu.parse_tab(INDIA_TAB, "Kerala After April 10")
    assert [(r.cuisine, r.category, r.name, r.ppp, r.mrp) for r in india] == [
        ("Kerala", "Kerala Special", "Appam", Decimal("2.50"), Decimal("3.25"))]


def test_old_price_tabs_and_tabs_without_prices_are_skipped():
    old = [row[:] for row in INDIA_TAB]
    old[4][4] = "1.00"
    plan, rows = plan_for([("Key", [["Tap Name", "Link"]]), ("Kerala Before April 10", old),
                           ("Kerala After April 10", INDIA_TAB)])
    assert plan.tabs_used == ["Kerala After April 10"]
    assert plan.tabs_skipped == ["Key (no PPP / MRP rows)", "Kerala Before April 10 (old prices)"]
    assert [r.ppp for r in rows] == [Decimal("2.50")]
    # Two tabs for one cuisine: the later one.
    plan, rows = plan_for([("Kerala May", old), ("Kerala June", INDIA_TAB)])
    assert plan.tabs_used == ["Kerala June"] and [r.ppp for r in rows] == [Decimal("2.50")]


async def test_the_ppp_is_updated_by_dish_and_the_mrp_left_alone(session, menu):
    plan, rows = plan_for([("Chettinad US - Apr'26", US_TAB), ("Kerala After April 10", INDIA_TAB)])
    await pu.plan(session, rows, plan_=plan)
    # "Drumstick Sambar - 16 OZ" is the menu's "Drumstick Sambar"; Beans is
    # already right; the new dish is not on the menu and is not added.
    assert plan.matched == 3 and plan.unchanged == 1
    assert plan.not_found == ["Chettinad / Sambar / Brand New Dish - 16 OZ"]
    assert {(c.name, c.old_ppp, c.new_ppp) for c in plan.changes} == {
        ("Drumstick Sambar", Decimal("8.16"), Decimal("9.00")),
        ("Appam", Decimal("2.12"), Decimal("2.50"))}
    assert await pu.apply(session, plan) == 2
    drumstick = menu_item(menu, "Drumstick Sambar")
    assert drumstick.cost_price == Decimal("9.00") and drumstick.price == Decimal("12.50")
    assert menu_item(menu, "Appam").cost_price == Decimal("2.50")
    names = (await session.execute(select(MenuItem.name))).scalars().all()
    assert "Brand New Dish - 16 OZ" not in names and len(names) == 4


async def test_the_mrp_changes_only_when_asked(session, menu):
    plan, rows = plan_for([("Chettinad US - Apr'26", US_TAB)])
    await pu.plan(session, rows, update_mrp=True, plan_=plan)
    await pu.apply(session, plan)
    drumstick = menu_item(menu, "Drumstick Sambar")
    assert drumstick.price == Decimal("13.00") and drumstick.cost_price == Decimal("9.00")
    assert plan.mrp_changes == 1 and "MRP changed on 1" in plan.summary()


async def test_a_price_never_lands_on_a_guess(session, menu):
    tab = US_TAB[:3] + [
        ["", "1", "Beans Sambar - 16 OZ", "", "", "1.00", "2.00"],
        ["", "2", "Beans Sambar - 32 OZ", "", "", "3.00", "4.00"],
        ["", "3", "Tomato Rasam", "", "", "5.00", "9.50"],
        ["", "4", "Tomato Rasam", "", "", "6.00", "9.50"],
    ]
    plan, rows = plan_for([("Chettinad US - Apr'26", tab),
                           ("Andhra US", [["", "Andhra Cuisine - US"], *US_TAB[2:4]])])
    await pu.plan(session, rows, plan_=plan)
    # Two sizes for one menu dish, and two prices for one dish: left alone.
    assert len(plan.not_found) == 2 and len(plan.conflicts) == 1 and not plan.changes
    assert "no cuisine called Andhra" in plan.warnings[0]
    assert await pu.apply(session, plan) == 0
    assert menu_item(menu, "Tomato Rasam").cost_price == Decimal("6.20")


async def test_a_size_in_the_middle_of_the_name_still_finds_the_dish(session, menu):
    """Shero US's sheet: "Rasam - 16 OZ ( No Side )" is the menu's "Rasam ( No
    Side )". DST India's menu names carry their size: a different size is a
    different dish, never matched."""
    menu_item(menu, "Tomato Rasam").name = "Tomato Rasam ( No Side )"
    menu_item(menu, "Appam").name = "Appam - 6 Nos"
    await session.flush()
    tab = US_TAB[:3] + [["", "1", "Tomato Rasam -16 OZ ( No Side )", "", "", "6.50", "9.50"]]
    kerala = INDIA_TAB[:4] + [["", "1", "1", "Appam - 4 Nos", "2.00", "3.00", "", ""]]
    plan, rows = plan_for([("Chettinad US", tab), ("Kerala After April 10", kerala)])
    await pu.plan(session, rows, plan_=plan)
    assert [(c.name, c.new_ppp) for c in plan.changes] == [
        ("Tomato Rasam ( No Side )", Decimal("6.50"))]
    assert plan.not_found == ["Kerala / Kerala Special / Appam - 4 Nos"]


async def test_the_checked_sheet_carries_over_to_the_update(session, menu):
    plan, rows = plan_for([("Kerala After April 10", INDIA_TAB)])
    payload = pu.dump_rows(rows, plan)
    again = pu.PricePlan()
    loaded = pu.load_rows(payload, again)
    assert loaded == rows and again.tabs_used == plan.tabs_used
    with pytest.raises(pu.PriceSheetError):
        pu.load_rows("not json", pu.PricePlan())


def test_an_excel_file_is_read_tab_by_tab():
    book = Workbook()
    sheet = book.active
    sheet.title = "Kerala After April 10"
    for row in INDIA_TAB:
        sheet.append([float(c) if c.replace(".", "").isdigit() else c for c in row])
    data = io.BytesIO()
    book.save(data)
    tabs = pu.workbook_tabs(data.getvalue())
    rows = pu.parse_tab(tabs[0][1], tabs[0][0])
    assert [(r.name, r.ppp, r.mrp) for r in rows] == [("Appam", Decimal("2.50"), Decimal("3.25"))]
    with pytest.raises(pu.PriceSheetError):
        pu.workbook_tabs(b"not a workbook")


def test_the_import_page_checks_then_updates(session, menu):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.admin import router
    from app.admin.deps import require_admin
    from app.core import tenants
    from app.db.models import AdminUser
    from app.db.session import get_session

    app = FastAPI()
    app.include_router(router)
    app.add_middleware(tenants.TenantPathMiddleware)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_admin] = lambda: AdminUser(
        email="ops@shero.test", name="Ops", password_hash="x", is_active=True)
    client = TestClient(app, follow_redirects=False)

    assert "Bulk price update" in client.get("/admin/import").text
    csv_text = "\n".join(",".join(f'"{c}"' for c in row) for row in US_TAB)
    checked = client.post("/admin/import/prices", files={
        "file": ("Chettinad.csv", csv_text.encode(), "text/csv")})
    assert checked.status_code == 200 and "data-price-check" in checked.text
    assert "Update 1 dish" in checked.text and "Brand New Dish" in checked.text
    assert menu_item(menu, "Drumstick Sambar").cost_price == Decimal("8.16")   # not yet
    payload = checked.text.split('name="payload" value="')[1].split('"')[0]
    import html

    done = client.post("/admin/import/prices/apply", data={"payload": html.unescape(payload)})
    assert done.status_code == 303
    assert menu_item(menu, "Drumstick Sambar").cost_price == Decimal("9.00")
    missing = client.post("/admin/import/prices", data={"sheet_url": ""})
    assert missing.status_code == 303
