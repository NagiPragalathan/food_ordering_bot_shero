"""DST India reads in its own terms: rupees, km, PIN codes and +91
(app/core/locale.py). Shero US's wording is checked here too, so each test
shows both businesses side by side; the rest of the suite already holds Shero
US to its exact text."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import locale, tenants
from app.db.models import AdminUser, DiscountType, Outlet, Promotion
from app.integrations.geo.geocoder import GeoPoint
from app.services import addresses, kitchen_admin, loyalty, out_of_area, promotions
from app.services.conversation import prompts
from app.services.conversation.handlers import delivery
from app.services.conversation.validators import clean_phone
from app.services.kitchen import ServiceCheck

DST = tenants.DST

INDIA_FORM = {
    "name": "Shero Anna Nagar", "address_line1": "12 2nd Avenue",
    "city": "Chennai", "state": "TN", "postal_code": "600040",
    "radius_miles": "8", "service_area_mode": "radius",
    "kitchen_whatsapp": "98400 12345", "kitchen_phone": "",
    "hours_mon_open": "09:00", "hours_mon_close": "21:00", "is_active": "1",
}


# --- money ---------------------------------------------------------------------
def test_the_cart_reads_in_rupees():
    lines = [{"name": "Sambar", "quantity": 2, "unit_price": "125.00"}]
    with tenants.use(DST):
        text = prompts.format_cart(lines)
    assert "2 x Sambar - ₹250.00" in text and "Subtotal: ₹250.00" in text
    assert "$" not in text
    assert "Subtotal: $250.00" in prompts.format_cart(lines)


def test_coupon_texts_read_in_rupees():
    flat = Promotion(code="SHERO50", discount_type=DiscountType.FLAT,
                     discount_value=Decimal("50"), min_order=Decimal("300"))
    percent = Promotion(code="SHERO20", discount_type=DiscountType.PERCENT,
                        discount_value=Decimal("20"), max_discount=Decimal("100"))
    with tenants.use(DST):
        assert promotions.headline(flat) == "₹50 off"
        assert promotions.default_tag(flat) == "₹50 OFF"
        assert "Min order ₹300" in promotions.conditions(flat)
        assert promotions.headline(percent) == "20% off, up to ₹100"
        assert promotions.amount_problem(flat, dish_total=Decimal("200")) == (
            "Add ₹100.00 more to use SHERO50 (minimum order ₹300.00).")
    assert promotions.headline(flat) == "$50 off"
    assert promotions.amount_problem(flat, dish_total=Decimal("200")) == (
        "Add $100.00 more to use SHERO50 (minimum order $300.00).")


def test_typed_amounts_take_either_currency():
    assert locale.amount_text("₹1,249") == "1249"
    assert locale.amount_text("Rs. 249.50") == "249.50"
    assert locale.amount_text("$16.53") == "16.53"


def test_the_loyalty_program_counts_in_the_business_currency():
    with tenants.use(DST):
        assert loyalty.Policy().currency == "INR"
    assert loyalty.Policy().currency == "USD"


# --- distance -------------------------------------------------------------------
def test_an_address_out_of_area_is_told_in_km():
    check = ServiceCheck(False, 18.5, "beyond", range_km=10.0)
    with tenants.use(DST):
        text = out_of_area.page_message(check, "12 2nd Avenue, 600040")
        info = out_of_area.details(check, "12 2nd Avenue, 600040", notified=False)
    assert "about 18.5 km away" in text and "mile" not in text
    assert (info["distance"], info["range"], info["unit"]) == (18.5, 10.0, "km")

    us = out_of_area.page_message(check, "30 Montgomery St, 07302")
    assert "about 11.5 miles away" in us
    assert out_of_area.details(check, "x", notified=False)["unit"] == "mi"


def test_a_kitchen_radius_is_typed_in_km_and_held_to_the_own_delivery_limit():
    with tenants.use(DST):
        assert kitchen_admin.max_radius() == 10.0          # OWN_DELIVERY_MAX_KM
        data = kitchen_admin.parse_form(INDIA_FORM)
        assert data.radius_miles == 8                      # km, as typed
        with pytest.raises(kitchen_admin.KitchenFormError,
                           match="delivery staff deliver at most 10 km"):
            kitchen_admin.parse_form({**INDIA_FORM, "radius_miles": "15"})
    with pytest.raises(kitchen_admin.KitchenFormError, match="Uber delivers at most 10 miles"):
        kitchen_admin.parse_form({**INDIA_FORM, "radius_miles": "15"})


async def test_an_india_kitchen_is_saved_in_km_with_its_country_and_91(session, monkeypatch):
    async def chennai(query, country=None):
        return GeoPoint(latitude=13.0850, longitude=80.2101, postal_code="600040")
    monkeypatch.setattr(kitchen_admin, "geocode_address", chennai)

    with tenants.use(DST):
        data = kitchen_admin.parse_form(INDIA_FORM)
        kitchen = await kitchen_admin.save(session, None, data)
    assert kitchen.delivery_radius_km == 8.0               # km stay km
    assert kitchen.country == "IN"
    assert kitchen.timezone == "Asia/Kolkata"
    assert kitchen.kitchen_whatsapp == "919840012345"

    us = kitchen_admin.parse_form({**INDIA_FORM, "kitchen_whatsapp": "(443) 801-1011"})
    assert us.kitchen_whatsapp == "(443) 801-1011"         # Shero US: as typed


@pytest.fixture
def admin(session):
    from app.admin import router
    from app.admin.deps import require_admin
    from app.db.session import get_session

    app = FastAPI()
    app.add_middleware(tenants.TenantPathMiddleware)
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_admin] = lambda: AdminUser(
        email="ops@shero.test", name="Ops", password_hash="x", is_active=True)
    return TestClient(app, follow_redirects=False)


def test_the_kitchen_form_speaks_km_and_pin_codes_for_dst_india(admin):
    page = admin.get("/dst/admin/kitchens/new").text
    assert "PIN code" in page and ">PIN<" in page and "ZIP" not in page
    assert "Delivery radius in km" in page and "Uber delivers" not in page
    assert "India (IST)" in page

    us = admin.get("/admin/kitchens/new").text
    assert "ZIP codes served" in us and "Delivery radius in miles" in us
    assert "Uber delivers at most" in us


async def test_the_kitchens_list_shows_km_for_dst_india(admin, session):
    session.add(Outlet(code="ANNA", name="Shero Anna Nagar", address_line1="12 2nd Avenue",
                       city="Chennai", state="TN", postal_code="600040", country="IN",
                       latitude=13.085, longitude=80.2101, delivery_radius_km=8.0,
                       timezone="Asia/Kolkata", service_area_mode="radius"))
    await session.flush()
    page = admin.get("/dst/admin/kitchens").text
    assert "8.0 km" in page and " mi<" not in page


# --- postal codes and phone numbers ------------------------------------------------
def test_the_bot_asks_for_a_pin_code():
    with tenants.use(DST):
        assert "type your PIN code" in prompts.ASK_LOCATION
        assert "valid PIN code" in prompts.LOCATION_UNREADABLE
        assert "PIN code" in prompts.ASK_AVAILABILITY_LOCATION
    assert "type your ZIP code" in prompts.ASK_LOCATION


class _Chat:
    """Just enough of FlowContext for the address step."""

    def __init__(self, customer, text):
        self.customer, self.text, self.sent, self.data, self.step = customer, text, [], {}, None

    async def reply_text(self, body):
        self.sent.append(body)

    def get(self, key):
        return self.data.get(key)

    def put(self, **values):
        self.data.update(values)

    def goto(self, step):
        self.step = step


async def test_a_pin_typed_with_the_street_is_the_one_delivered_to(customer):
    with tenants.use(DST):
        chat = _Chat(customer, "12 2nd Avenue, Anna Nagar, Chennai 600040")
        await delivery.handle_address(chat)
    assert customer.postal_code == "600040"

    # Shero US still reads a 5-digit ZIP (and not a 6-digit PIN).
    chat = _Chat(customer, "12 Maple St, Edison NJ 08837")
    await delivery.handle_address(chat)
    assert customer.postal_code == "08837"
    assert not locale.trailing_postal_code().search("Anna Nagar 600040")


def test_a_bare_10_digit_number_gets_91_in_india_only():
    with tenants.use(DST):
        assert clean_phone("98400 12345") == "919840012345"
        assert clean_phone("+91 98400 12345") == "919840012345"
    assert clean_phone("(732) 555-0142") == "7325550142"


def test_a_saved_address_asks_for_the_pin_code():
    with tenants.use(DST):
        with pytest.raises(addresses.AddressError, match="Enter your PIN code."):
            addresses.clean({"address_line1": "12 2nd Avenue"})
    with pytest.raises(addresses.AddressError, match="Enter your ZIP code."):
        addresses.clean({"address_line1": "12 Maple St"})
