"""The web page's address routes, the map pin lookup and the WhatsApp link.

Routes are called as plain functions with a real session, the way the other
service tests work, rather than through an HTTP client.
"""

from __future__ import annotations

import pytest

from app.api.routes import order_addresses, order_web
from app.core.config import settings
from app.db.models import Customer
from app.integrations.geo import geocoder
from app.integrations.geo.geocoder import GeoPoint
from app.services import addresses, order_link


class FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch):
    monkeypatch.setattr(settings, "admin_session_secret", "test-secret")


def _token(customer) -> str:
    return order_link.build_token(customer.id)


async def test_the_list_shows_the_whatsapp_number_as_the_contact(session, customer):
    customer.contact_number = "19999999999"
    data = await order_addresses.list_addresses(_token(customer), session=session)

    assert data["ok"] is True
    assert data["contact_number"] == customer.whatsapp_number
    assert data["labels"] == ["Home", "Office", "Other"]


async def test_save_then_list(session, customer):
    customer.address_line1 = None
    token = _token(customer)
    saved = await order_addresses.save_address(
        FakeRequest({"label": "Office", "address_line1": "1 Wood Ave",
                     "postal_code": "08830"}), token, session=session)
    assert saved["ok"] is True

    listed = await order_addresses.list_addresses(token, session=session)
    assert [a["label"] for a in listed["addresses"]] == ["Office"]


async def test_a_bad_address_comes_back_as_a_message(session, customer):
    data = await order_addresses.save_address(
        FakeRequest({"address_line1": "", "postal_code": "08830"}),
        _token(customer), session=session)
    assert data == {"ok": False, "error": "Enter your street address."}


async def test_a_dead_link_gets_the_expired_message(session, customer):
    data = await order_addresses.list_addresses("forged-token", session=session)
    assert data["ok"] is False
    assert "expired" in data["error"]


async def test_the_pin_lookup_fills_street_and_zip(session, customer, monkeypatch):
    async def fake_reverse(lat, lng):
        return GeoPoint(latitude=lat, longitude=lng, street="12 Maple Street",
                        postal_code="08820", city="Edison")

    monkeypatch.setattr(order_addresses, "reverse_geocode", fake_reverse)
    data = await order_addresses.locate(_token(customer), 40.52, -74.41, session=session)
    assert data["address_line1"] == "12 Maple Street"
    assert data["postal_code"] == "08820"


async def test_a_pin_in_the_sea_asks_the_customer_to_type(session, customer, monkeypatch):
    async def nothing(lat, lng):
        return None

    monkeypatch.setattr(order_addresses, "reverse_geocode", nothing)
    data = await order_addresses.locate(_token(customer), 0.0, 0.0, session=session)
    assert data["ok"] is False
    assert "type the address" in data["error"]


async def test_search_moves_the_pin_and_fills_the_form(session, customer, monkeypatch):
    async def fake_search(q):
        assert q == "12 maple st edison"
        return GeoPoint(latitude=40.5201, longitude=-74.4102, street="12 Maple Street",
                        postal_code="08820")

    monkeypatch.setattr(order_addresses, "geocode_address", fake_search)
    data = await order_addresses.search(_token(customer), q="12 maple st edison",
                                        session=session)
    assert data == {"ok": True, "lat": 40.5201, "lng": -74.4102,
                    "address_line1": "12 Maple Street", "postal_code": "08820",
                    "formatted": ""}


async def test_a_search_with_no_match_says_what_to_try(session, customer, monkeypatch):
    async def nothing(q):
        return None

    monkeypatch.setattr(order_addresses, "geocode_address", nothing)
    data = await order_addresses.search(_token(customer), q="nowhere", session=session)
    assert data["ok"] is False
    assert "town" in data["error"]


async def test_reverse_lookup_refuses_impossible_coordinates():
    assert await geocoder.reverse_geocode(123.0, 10.0) is None


async def test_checking_delivery_to_someone_elses_address_is_refused(session, customer):
    stranger = Customer(whatsapp_number="14155550199")
    session.add(stranger)
    await session.flush()
    theirs = await addresses.save(session, stranger, {
        "label": "Home", "address_line1": "9 Elm Road", "postal_code": "08820"})

    data = await order_web.check_address(
        FakeRequest({"address_id": str(theirs.id)}), _token(customer), session=session)
    assert data["ok"] is False
    assert "could not be found" in data["error"]


# --- going back to WhatsApp after ordering ------------------------------------------
def test_the_whatsapp_link_uses_the_business_number(monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_business_number", "+1 (443) 801-1011")
    assert order_link.whatsapp_chat_url() == "https://wa.me/14438011011"


def test_no_business_number_means_no_button(monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_business_number", "")
    assert order_link.whatsapp_chat_url() == ""


# --- the address-only page (order summary -> Update location) ----------------
def _client(session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.db.session import get_session

    app = FastAPI()
    app.include_router(order_web.router)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


async def test_the_location_page_is_the_address_step_only(session, customer, outlet):
    page = _client(session).get(f"/order/{_token(customer)}/location")
    assert page.status_code == 200
    assert "Update delivery address" in page.text
    # Same checkout as the menu page, not a copy of it.
    assert "SheroCheckout" in page.text and "SheroAddressBook" in page.text
    assert 'id="cuisines"' not in page.text


async def test_the_menu_page_still_renders_with_the_shared_checkout(session, customer, outlet):
    page = _client(session).get(f"/order/{_token(customer)}")
    assert page.status_code == 200
    assert "SheroCheckout" in page.text and 'id="cuisines"' in page.text


async def test_a_dead_location_link_shows_the_expired_page(session, customer):
    assert _client(session).get("/order/forged/location").status_code == 410


@pytest.mark.parametrize("linked,module,street,zip_code", [
    ("zoho_lead_id", "Leads", "Street", "Zip_Code"),
    ("zoho_contact_id", "Contacts", "Mailing_Street", "Mailing_Zip"),
])
async def test_a_saved_address_goes_straight_to_the_zoho_record(
        session, customer, monkeypatch, linked, module, street, zip_code):
    """Update location reaches the Lead - or the Contact, once they have paid."""
    from app.services import crm_sync

    setattr(customer, linked, "z-1")
    writes = []

    async def update_record(mod, zoho_id, fields):
        writes.append((mod, zoho_id, fields))
    monkeypatch.setattr(crm_sync.crm, "update_record", update_record)

    saved = await order_addresses.save_address(
        FakeRequest({"label": "Office", "address_line1": "1 Wood Ave",
                     "apartment_unit": "3C", "postal_code": "08830"}),
        _token(customer), session=session)
    assert saved["ok"] is True

    [(mod, zoho_id, fields)] = writes
    assert (mod, zoho_id) == (module, "z-1")
    assert fields[street] == "1 Wood Ave" and fields[zip_code] == "08830"
    assert fields["Address_Line_2"] == "3C"
