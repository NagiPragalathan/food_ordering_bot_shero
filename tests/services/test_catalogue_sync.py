"""Menu dishes -> Zoho Products and kitchens -> Zoho Vendors
(services/catalogue_sync.py, and the Products / Vendors side of
integrations/zoho/crm.py)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.exceptions import IntegrationError
from app.integrations.zoho import crm
from app.integrations.zoho import fields as f
from app.services import catalogue_sync

pytestmark = pytest.mark.asyncio


@pytest.fixture
def zoho(monkeypatch):
    """A fake Zoho: records writes; `found` is what a search by code returns
    and `update_error` what an update raises."""
    class Calls(list):
        found: dict | None = None
        update_error: Exception | None = None

    calls = Calls()

    async def find_product_by_code(code):
        calls.append(("find_product", code))
        return calls.found

    async def create_product(fields):
        calls.append(("create_product", fields))
        return "prod-new"

    async def update_product(zoho_id, fields):
        if calls.update_error:
            raise calls.update_error
        calls.append(("update_product", zoho_id, fields))

    async def find_vendor(code, name):
        calls.append(("find_vendor", code, name))
        return calls.found

    async def create_vendor(fields):
        calls.append(("create_vendor", fields))
        return "vendor-new"

    async def update_vendor(zoho_id, fields):
        if calls.update_error:
            raise calls.update_error
        calls.append(("update_vendor", zoho_id, fields))

    for name, fn in {"find_product_by_code": find_product_by_code,
                     "create_product": create_product, "update_product": update_product,
                     "find_vendor": find_vendor, "create_vendor": create_vendor,
                     "update_vendor": update_vendor}.items():
        monkeypatch.setattr(catalogue_sync.crm, name, fn)
    return calls


def _writes(calls, kind):
    return [c for c in calls if c[0] == kind]


async def _dish(session, code):
    """A menu row with its category and cuisine loaded, as the sync needs."""
    return (await session.execute(catalogue_sync.dishes_with_labels([code]))).scalar_one()


GONE = IntegrationError("zoho", "record write failed", payload={
    "code": "INVALID_DATA", "details": {"api_name": "id"},
    "message": "the id given seems to be invalid"})


# --- Products ----------------------------------------------------------------
async def test_a_dish_becomes_a_product_and_remembers_its_id(session, menu, zoho):
    item = await _dish(session, "kerala-sambar-appam")

    assert await catalogue_sync.ensure_product(item) == "prod-new"

    assert item.zoho_product_id == "prod-new"
    (_, fields), = _writes(zoho, "create_product")
    assert fields[f.P_NAME] == "Appam" and fields[f.P_CODE] == "kerala-sambar-appam"
    assert fields[f.P_UNIT_PRICE] == Decimal("3.25") and fields[f.P_ACTIVE] is True
    assert fields[f.P_CUISINE] == "Kerala" and fields[f.P_DISH_CATEGORY] == "Sambar"
    assert fields[f.P_PACK_SIZE] == "6 pieces" and fields[f.P_SERVES] == "Serves 2"
    assert fields[f.P_PHOTO_URL] is None          # no public photo URL


async def test_a_product_already_in_zoho_is_found_by_code_and_updated(session, menu, zoho):
    zoho.found = {"id": "prod-9", f.P_NAME: "Appam"}
    item = await _dish(session, "kerala-sambar-appam")

    assert await catalogue_sync.ensure_product(item) == "prod-9"

    assert _writes(zoho, "create_product") == []
    (_, zoho_id, fields), = _writes(zoho, "update_product")
    assert zoho_id == "prod-9" and fields[f.P_CODE] == "kerala-sambar-appam"
    assert item.zoho_product_id == "prod-9"


async def test_a_linked_product_is_updated_by_id_without_searching(session, menu, zoho):
    item = await _dish(session, "kerala-sambar-appam")
    item.zoho_product_id = "prod-3"
    item.is_available = False

    assert await catalogue_sync.ensure_product(item) == "prod-3"

    assert _writes(zoho, "find_product") == []
    (_, zoho_id, fields), = _writes(zoho, "update_product")
    assert zoho_id == "prod-3" and fields[f.P_ACTIVE] is False


async def test_a_linked_product_deleted_in_zoho_is_recreated(session, menu, zoho):
    item = await _dish(session, "kerala-sambar-appam")
    item.zoho_product_id = "prod-gone"
    zoho.update_error = GONE

    assert await catalogue_sync.ensure_product(item) == "prod-new"

    assert _writes(zoho, "find_product") == [("find_product", "kerala-sambar-appam")]
    assert len(_writes(zoho, "create_product")) == 1
    assert item.zoho_product_id == "prod-new"


async def test_zoho_being_down_returns_none_and_keeps_the_link(session, menu, zoho):
    item = await _dish(session, "kerala-sambar-appam")
    item.zoho_product_id = "prod-3"
    zoho.update_error = IntegrationError("zoho", "network error")

    assert await catalogue_sync.ensure_product(item) is None
    assert item.zoho_product_id == "prod-3"


# --- Vendors -----------------------------------------------------------------
async def test_a_kitchen_becomes_a_vendor(outlet, zoho):
    outlet.kitchen_whatsapp = "17325550199"

    assert await catalogue_sync.ensure_vendor(outlet) == "vendor-new"

    assert outlet.zoho_vendor_id == "vendor-new"
    (_, fields), = _writes(zoho, "create_vendor")
    assert fields[f.V_NAME] == "Shero Edison" and fields[f.V_OUTLET_CODE] == "EDISON"
    assert fields[f.V_STREET] == "123 Oak Tree Road" and fields[f.V_CITY] == "Edison"
    assert fields[f.V_STATE] == "NJ" and fields[f.V_ZIP] == "08820"
    assert fields[f.V_COUNTRY] == "United States"
    assert fields[f.V_LATITUDE] == 40.5187 and fields[f.V_LONGITUDE] == -74.4121
    assert fields[f.V_KITCHEN_WHATSAPP] == "+17325550199"
    assert fields[f.V_DELIVERY_RADIUS_KM] == 12.0
    assert "Andhra, Kerala" in fields[f.V_DESCRIPTION]


async def test_a_vendor_already_in_zoho_is_linked_not_duplicated(outlet, zoho):
    zoho.found = {"id": "vendor-9"}
    assert await catalogue_sync.ensure_vendor(outlet) == "vendor-9"
    assert _writes(zoho, "find_vendor") == [("find_vendor", "EDISON", "Shero Edison")]
    assert _writes(zoho, "create_vendor") == []


# --- the admin's Sync to Zoho --------------------------------------------------
async def test_the_sync_counts_dishes_and_kitchens(session, menu, outlet, zoho):
    assert await catalogue_sync.push_catalogue(session) == (4, 1, [])
    assert all(item.zoho_product_id == "prod-new" for item in menu.values())
    assert outlet.zoho_vendor_id == "vendor-new"


async def test_the_sync_reports_what_failed(session, menu, outlet, zoho, monkeypatch):
    async def broken(_fields):
        raise IntegrationError("zoho", "INVALID_DATA")
    monkeypatch.setattr(catalogue_sync.crm, "create_product", broken)

    dishes, kitchens, problems = await catalogue_sync.push_catalogue(session)

    assert (dishes, kitchens) == (0, 1)
    assert problems == ["Could not sync 4 dish(es): Appam, Beans Sambar, Drumstick Sambar, "
                        "Tomato Rasam - see the server log."]


# --- the Zoho layer itself ------------------------------------------------------
async def test_a_taken_product_name_gets_the_cuisine_and_category_added(monkeypatch):
    sent = []

    async def create_record(_module, payload):
        sent.append(payload)
        if len(sent) == 1:
            raise IntegrationError("zoho", "record write failed", payload={
                "code": "DUPLICATE_DATA", "details": {"api_name": f.P_NAME},
                "message": "duplicate data"})
        return "prod-2"
    monkeypatch.setattr(crm.zoho_client, "create_record", create_record)

    fields = {f.P_NAME: "Sambar", f.P_CODE: "kerala-curries-sambar",
              f.P_CUISINE: "Kerala", f.P_DISH_CATEGORY: "Curries"}
    assert await crm.create_product(fields) == "prod-2"
    assert [p[f.P_NAME] for p in sent] == ["Sambar", "Sambar (Kerala, Curries)"]


async def test_a_state_zoho_does_not_know_is_dropped_from_the_vendor(monkeypatch):
    sent = []

    async def create_record(_module, payload):
        sent.append(payload)
        if f.V_STATE in payload:
            raise IntegrationError("zoho", "POST returned 400", status_code=400, payload={
                "data": [{"code": "INVALID_DATA",
                          "details": {"api_name": f.V_STATE, "expected_data_type": "picklist"},
                          "message": "invalid data", "status": "error"}]})
        return "vendor-2"
    monkeypatch.setattr(crm.zoho_client, "create_record", create_record)

    fields = {f.V_NAME: "Shero Edison", f.V_CITY: "Edison", f.V_STATE: "NJ",
              f.V_COUNTRY: "United States"}
    assert await crm.create_vendor(fields) == "vendor-2"
    assert len(sent) == 2
    assert f.V_STATE not in sent[1] and sent[1][f.V_COUNTRY] == "United States"


async def test_any_other_vendor_error_still_fails(monkeypatch):
    async def create_record(_module, _payload):
        raise IntegrationError("zoho", "record write failed", payload={
            "code": "MANDATORY_NOT_FOUND", "details": {"api_name": f.V_NAME}})
    monkeypatch.setattr(crm.zoho_client, "create_record", create_record)
    with pytest.raises(IntegrationError):
        await crm.create_vendor({f.V_NAME: "X", f.V_STATE: "NJ"})


def test_a_missing_record_is_recognised_in_both_error_shapes():
    assert crm.is_missing_record(GONE)
    assert crm.is_missing_record(IntegrationError("zoho", "PUT returned 404", status_code=404))
    wrapped = IntegrationError("zoho", "PUT returned 400", status_code=400, payload={
        "data": [{"code": "INVALID_DATA", "details": {"api_name": "id"}, "status": "error"}]})
    assert crm.is_missing_record(wrapped)
    assert not crm.is_missing_record(IntegrationError("zoho", "network error"))


def test_a_cart_line_is_read_into_code_name_quantity_and_price():
    assert f.parse_line({"retailer_id": "kerala-sambar-appam", "name": "Appam",
                         "quantity": "3", "unit_price": "3.25"}) == (
        "kerala-sambar-appam", "Appam", 3, Decimal("3.25"))
    assert f.parse_line({"name": "Sambar"}) == ("Sambar", "Sambar", 1, Decimal("0"))
    assert f.parse_line({"name": "Sambar", "quantity": "two"}) is None


def test_country_codes_become_zoho_country_names():
    assert f.country_name("us") == "United States" and f.country_name("IN") == "India"
    assert f.country_name("ZZ") == "ZZ" and f.country_name("") is None
