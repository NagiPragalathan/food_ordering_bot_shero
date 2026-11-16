"""The menu from the Shero ERP's item exports (services/erp_import.py), and
the Import page that uploads them."""

from __future__ import annotations

import io
from decimal import Decimal

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select

from app.core import tenants
from app.db.models import Category, Cuisine, MenuItem
from app.services import erp_import

HEADER = ["Item_code", "Name", "Category", "Brand", "Description", "image", "Type", "Unit",
          "Sku", "Food_type", "Hsn", "Prep_time", "Pp_price", "Mrp", "Package_Charge",
          "Price_variation", "Video_link"]
PHOTO = "https://sherohomeerp-shero.s3.ap-south-1.amazonaws.com/items/{}.jpg"


def export(*rows, cost=True) -> bytes:
    """An ERP "Item Details" export: title rows, the header on row 5, dishes."""
    header = HEADER if cost else [h for h in HEADER if h != "Pp_price"]
    book = Workbook()
    sheet = book.active
    sheet.append([])
    sheet.append(["Shero Home - Item Details:"])
    sheet.append(["Export Date:14-11-2026"])
    sheet.append([])
    sheet.append(header)
    for code, name, category, brand, price, *rest in rows:
        values = {"Item_code": code, "Name": name, "Category": category, "Brand": brand,
                  "Description": f"About {name}", "image": PHOTO.format(code.lower()),
                  "Type": "sale", "Sku": code[-3:], "Food_type": "Veg", "Hsn": "123",
                  "Pp_price": rest[0] if rest else None, "Mrp": price, "Package_Charge": 5,
                  "Price_variation": "Price 3"}
        sheet.append([values.get(h) for h in header])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


ANDHRA = export(("ITM0000010", "Dosakaya Sambar - 450 ml", "Day House Special ", "Andhra", "160"),
                ("ITM0000631", "Atukula Pulihora - 750 ml", "Rice & Tiffin", "Andhra", "255"),
                cost=False)
# The full catalogue, named after Kerala: every brand, with cost prices.
KERALA = export(("ITM0000010", "Dosakaya Sambar - 450 ml", "Day House Special", "Andhra", "160", "64"),
                ("ITM0000028", "Parippu Curry - 450 ml", "Kerala Special", "Kerala", "198", "81"),
                ("ITM0000029", "Avial - 450 ml", "Kerala Special", "Kerala ", "", "70"),
                ("ITM0000078", "Peanut Chutney - 250 ml", "Chutney", "Chettinad", "209", "73"))
NORTH = export(("ITM0000184", "White Rice - 750 ml", "Day House Special", "South Indian", "127", "44"))


async def dishes(session) -> dict[str, MenuItem]:
    rows = (await session.execute(select(MenuItem))).scalars()
    return {d.retailer_id: d for d in rows}


def test_an_export_is_read_from_its_header_row():
    rows = erp_import.read_export(KERALA, "Shero Home Food - Kerala.xlsx")
    assert [r.code for r in rows] == ["ITM0000010", "ITM0000028", "ITM0000029", "ITM0000078"]
    first = rows[0]
    assert (first.category, first.brand, first.price, first.cost) == \
        ("Day House Special", "Andhra", Decimal("160.00"), Decimal("64.00"))
    assert first.image.startswith("https://sherohomeerp")
    assert rows[2].price is None                       # no Mrp


def test_anything_else_is_refused_with_a_reason():
    with pytest.raises(erp_import.ErpImportError, match="could not be opened as an Excel"):
        erp_import.read_export(b"not a workbook", "menu.xlsx")
    book = Workbook()
    book.active.append(["Name", "Price"])
    out = io.BytesIO()
    book.save(out)
    with pytest.raises(erp_import.ErpImportError, match="not an ERP item export"):
        erp_import.read_export(out.getvalue(), "menu.xlsx")


@pytest.mark.parametrize("filename,cuisine", [
    ("Shero Home Food - Andhra.xlsx", "Andhra"),
    ("Shero Home Food - North Indian.xlsx", "North Indian"),
    ("Shero Home Food - Kerala (1).xlsx", "Kerala"),
    ("Chettinad.xlsx", "Chettinad"),
])
def test_the_cuisine_is_named_after_the_file(filename, cuisine):
    assert erp_import.cuisine_from_filename(filename) == cuisine


async def test_each_file_is_a_cuisine_in_rupees(session):
    with tenants.use(tenants.DST):
        result = await erp_import.import_exports(session, [
            ("Shero Home Food - Andhra.xlsx", ANDHRA, ""),
            ("Shero Home Food - Kerala.xlsx", KERALA, ""),
            ("Shero Home Food - North Indian.xlsx", NORTH, ""),
        ])
        found = await dishes(session)
    assert result.cuisines == ["Andhra", "Kerala", "North Indian"]
    # Kerala's file is the whole catalogue: only its own brand is taken.
    assert set(found) == {"itm0000010", "itm0000631", "itm0000028", "itm0000029", "itm0000184"}
    sambar = found["itm0000010"]
    assert (sambar.price, sambar.currency, sambar.is_available) == (Decimal("160.00"), "INR", True)
    # Andhra's export has no cost; the catalogue's Pp_price fills it in.
    assert sambar.cost_price == Decimal("64.00")
    assert found["itm0000029"].is_available is False          # no Mrp: not sold
    assert any("took its 2 Kerala dishes" in w for w in result.warnings)
    assert any('marked brand "South Indian"' in w for w in result.warnings)
    assert any("no Mrp" in w for w in result.warnings)


async def test_a_mixed_file_with_no_matching_brand_is_refused(session):
    with pytest.raises(erp_import.ErpImportError, match="several brands"):
        await erp_import.import_exports(session, [("Shero Home Food - Punjabi.xlsx", KERALA, "")])


async def test_importing_again_updates_the_same_dishes(session):
    await erp_import.import_exports(session, [("Shero Home Food - Andhra.xlsx", ANDHRA, "")])
    newer = export(("ITM0000010", "Dosakaya Sambar - 450 ml", "Day House Special", "Andhra", "175"),
                   cost=False)
    result = await erp_import.import_exports(session, [("Shero Home Food - Andhra.xlsx", newer, "")])
    found = await dishes(session)
    assert result.items_updated == 1 and result.items_created == 0
    assert found["itm0000010"].price == Decimal("175.00")
    assert "itm0000631" in found                    # not replacing: kept


async def test_replacing_deletes_everything_not_in_the_files(session, menu):
    """The old menu goes: its dishes, and the categories and cuisines left empty."""
    before = await session.scalar(select(func.count(MenuItem.id)))
    assert before >= 4
    result = await erp_import.import_exports(
        session, [("Shero Home Food - Andhra.xlsx", ANDHRA, "")], replace=True)
    found = await dishes(session)
    assert set(found) == {"itm0000010", "itm0000631"}
    assert result.items_removed == before
    assert [c.name for c in (await session.execute(select(Cuisine))).scalars()] == ["Andhra"]
    assert {c.name for c in (await session.execute(select(Category))).scalars()} == \
        {"Day House Special", "Rice & Tiffin"}


async def test_photos_are_copied_to_our_store_a_batch_at_a_time(session, monkeypatch):
    await erp_import.import_exports(session, [("Shero Home Food - Andhra.xlsx", ANDHRA, "")])
    assert await erp_import.photos_waiting(session) == 2

    async def stored(urls):
        return {url: f"/media/{url.rsplit('/', 1)[-1]}" for url in urls[:1]}

    monkeypatch.setattr(erp_import.media, "fetch_many", stored)
    assert await erp_import.store_remote_photos(session, limit=5) == 1
    assert await erp_import.photos_waiting(session) == 1


async def test_a_stored_photo_survives_a_reimport(session, monkeypatch):
    await erp_import.import_exports(session, [("Shero Home Food - Andhra.xlsx", ANDHRA, "")])
    dish = (await dishes(session))["itm0000010"]
    local = erp_import.media.local_path_for(dish.image_url, "image/jpeg")
    dish.image_url = f"{erp_import.media.MEDIA_URL_PREFIX}/{local.name}"
    await session.flush()
    await erp_import.import_exports(session, [("Shero Home Food - Andhra.xlsx", ANDHRA, "")])
    assert (await dishes(session))["itm0000010"].image_url.endswith(local.name)


def test_the_import_page_uploads_the_exports_for_dst(session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.admin import router
    from app.admin.deps import require_admin
    from app.db.models import AdminUser
    from app.db.session import get_session

    app = FastAPI()
    app.include_router(router)
    app.add_middleware(tenants.TenantPathMiddleware)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_admin] = lambda: AdminUser(
        email="ops@shero.test", name="Ops", password_hash="x", is_active=True)
    client = TestClient(app, follow_redirects=False)

    page = client.get("/dst/admin/import")
    assert page.status_code == 200 and "Upload ERP item exports" in page.text
    sent = client.post("/dst/admin/import/erp", data={"replace": "on"}, files=[
        ("files", ("Shero Home Food - Andhra.xlsx", ANDHRA, "application/octet-stream")),
        ("files", ("Shero Home Food - North Indian.xlsx", NORTH, "application/octet-stream")),
    ])
    assert sent.status_code == 303 and sent.headers["location"].endswith("/dst/admin/import")
    assert "Imported 2 cuisine(s)" in sent.cookies.get("shero_flash", "") or \
        "Imported" in (sent.headers.get("set-cookie") or "")


async def test_a_big_cuisine_is_shown_a_page_at_a_time(session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.admin import router
    from app.admin.deps import require_admin
    from app.db.models import AdminUser
    from app.db.session import get_session

    many = export(*[(f"ITM{n:07d}", f"Dish {n}", "Day House Special", "Chettinad", "100")
                    for n in range(1, 161)])
    with tenants.use(tenants.DST):
        await erp_import.import_exports(session, [("Shero Home Food - Chettinad.xlsx", many, "")])
    app = FastAPI()
    app.include_router(router)
    app.add_middleware(tenants.TenantPathMiddleware)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_admin] = lambda: AdminUser(
        email="ops@shero.test", name="Ops", password_hash="x", is_active=True)
    client = TestClient(app)
    first = client.get("/dst/admin/menu?cuisine=chettinad").text
    assert "160 item(s)" in first and "Page 1 of 2" in first
    assert "Dish 150<" in first or "Dish 150\"" in first or "Dish 150 " in first or "Dish 150" in first
    second = client.get("/dst/admin/menu?cuisine=chettinad&page=2").text
    assert "Page 2 of 2" in second and "Dish 160" in second and "Dish 1\"" not in second
