"""Where an order is going, for the kitchen distance check
(delivery_address.locate), and the furthest any kitchen delivers
(kitchen.reach_km, DELIVERY_MAX_MILES).

SHO-261108-7QW2Q: the ordering page's map opens on the kitchen, and the pin
was left there; the customer typed 5300 Wisconsin Avenue NW, Washington DC
20015. Our check measured the pin, 4 miles, and let the order through. The
address was 23.7 miles away - beyond the kitchen's 20-mile radius, and beyond
the 10 miles Uber delivers. Now the typed address decides where an order is
going, and no kitchen reaches past the cap whatever its own radius says.
"""

from __future__ import annotations

import pytest

from app.api.routes import order_web
from app.core.config import settings
from app.integrations.gallabox.sender import use_sender
from app.integrations.geo.geocoder import GeoPoint
from app.services import addresses, delivery_address, kitchen_admin, order_link
from app.services import kitchen as kitchen_service
from tests.services.test_kitchen_admin import FORM
from tests.services.test_out_of_area import FakeRequest, Recorder

# Beside the Edison test kitchen (40.5187, -74.4121): where its map opens.
BY_THE_KITCHEN = (40.5200, -74.4100)
JERSEY_CITY = GeoPoint(40.7215, -74.0466, postal_code="07302", street="30 Montgomery St",
                       source="google")
WOOD_AVE = GeoPoint(40.5205, -74.4105, postal_code="08830", street="1 Wood Ave",
                    source="google")


@pytest.fixture
def streets(monkeypatch):
    """What the geocoder finds for a typed "street, ZIP"."""
    found: dict[str, GeoPoint] = {}

    async def lookup(text, country=None):
        return found.get(text)
    monkeypatch.setattr(delivery_address, "geocode_address", lookup)
    return found


@pytest.fixture
def zips(monkeypatch):
    found: dict[str, GeoPoint] = {}

    async def lookup(code, country=None):
        return found.get(code)
    monkeypatch.setattr(delivery_address, "geocode_postal_code", lookup)
    return found


async def _locate(street, zip_code, pin=BY_THE_KITCHEN):
    lat, lng = pin if pin else (None, None)
    return await delivery_address.locate(street=street, postal_code=zip_code,
                                         latitude=lat, longitude=lng)


# --- the point measured -----------------------------------------------------------------
async def test_a_pin_left_by_the_kitchen_is_not_the_typed_address(streets):
    streets["30 Montgomery St, 07302"] = JERSEY_CITY
    assert await _locate("30 Montgomery St", "07302") == JERSEY_CITY


async def test_a_pin_at_the_typed_address_is_kept_for_the_exact_door(streets):
    streets["1 Wood Ave, 08830"] = WOOD_AVE
    point = await _locate("1 Wood Ave", "08830")
    assert (point.source, point.latitude, point.longitude) == ("pin", *BY_THE_KITCHEN)


async def test_a_street_not_found_is_checked_against_its_zip(streets, zips):
    zips["07302"] = GeoPoint(40.7180, -74.0450, postal_code="07302")
    assert await _locate("30 Montgomery St", "07302") == zips["07302"]


async def test_a_match_in_another_zip_or_without_a_house_number_is_not_trusted(streets):
    streets["30 Montgomery St, 07302"] = GeoPoint(40.75, -73.99, postal_code="10001",
                                                  street="30 Montgomery St")
    streets["Montgomery St, 07302"] = GeoPoint(40.72, -74.04, postal_code="07302",
                                               street="Montgomery Street")
    for street in ("30 Montgomery St", "Montgomery St"):
        assert (await _locate(street, "07302")).source == "pin"    # nothing better to go on


async def test_without_a_pin_the_typed_address_is_used(streets):
    streets["30 Montgomery St, 07302"] = JERSEY_CITY
    assert await _locate("30 Montgomery St", "07302", pin=None) == JERSEY_CITY
    assert await _locate("", "", pin=None) is None


# --- the ordering page ------------------------------------------------------------------
@pytest.mark.parametrize("step", ["check_address", "quote", "confirm"])
async def test_the_page_refuses_an_address_typed_far_from_its_pin(
        session, customer, outlet, streets, monkeypatch, step):
    monkeypatch.setattr(settings, "admin_session_secret", "test-secret")
    monkeypatch.setattr(order_web, "_clean_cart", lambda lines: [{"retailer_id": "x"}])
    streets["30 Montgomery St, 07302"] = JERSEY_CITY
    far = await addresses.save(session, customer, {
        "label": "Home", "address_line1": "30 Montgomery St", "postal_code": "07302",
        "latitude": BY_THE_KITCHEN[0], "longitude": BY_THE_KITCHEN[1]})

    with use_sender(Recorder()):
        data = await getattr(order_web, step)(
            FakeRequest({"address_id": str(far.id), "slot_id": "x"}),
            order_link.build_token(customer.id), session=session)
    assert data["ok"] is False and data["serviceable"] is False
    assert "30 Montgomery St" in data["error"]


# --- no kitchen past the cap ------------------------------------------------------------
def _miles_north(outlet, miles: float) -> GeoPoint:
    return GeoPoint(outlet.latitude + miles * kitchen_service.KM_PER_MILE / 111.195,
                    outlet.longitude)


async def test_no_kitchen_delivers_past_the_cap_whatever_its_radius(outlet, monkeypatch):
    outlet.delivery_radius_km = kitchen_service.km(20)              # set to 20 miles
    [inside] = kitchen_service.coverage([outlet], _miles_north(outlet, 9.5), "")
    [beyond] = kitchen_service.coverage([outlet], _miles_north(outlet, 11), "")
    assert inside.is_serviceable and not beyond.is_serviceable
    assert kitchen_service.reach_km(outlet) == kitchen_service.km(10)

    monkeypatch.setattr(settings, "delivery_max_miles", 0)          # 0: no cap
    [beyond] = kitchen_service.coverage([outlet], _miles_north(outlet, 11), "")
    assert beyond.is_serviceable


def test_a_smaller_radius_still_counts(outlet):
    assert kitchen_service.reach_km(outlet) == outlet.delivery_radius_km     # 12 km < 10 mi


def test_the_kitchen_form_refuses_a_radius_past_the_cap():
    with pytest.raises(kitchen_admin.KitchenFormError, match="at most 10 miles"):
        kitchen_admin.parse_form({**FORM, "radius_miles": "20"})
    assert kitchen_admin.parse_form({**FORM, "radius_miles": "8"}).radius_miles == 8
