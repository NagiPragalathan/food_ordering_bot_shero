"""Adding and editing kitchens from the admin Kitchens page
(services/kitchen_admin.py)."""

from __future__ import annotations

import pytest

from app.integrations.geo.geocoder import GeoPoint
from app.services import kitchen_admin

pytestmark = pytest.mark.asyncio

FORM = {
    "name": "Shero Elkridge", "address_line1": "6639 Cambria Terrace",
    "city": "Elkridge", "state": "MD", "postal_code": "21075",
    "radius_miles": "10", "service_area_mode": "radius",
    "kitchen_whatsapp": "14438011011", "kitchen_phone": "",
    "hours_mon_open": "09:00", "hours_mon_close": "21:00", "is_active": "1",
}


@pytest.fixture
def geocoded(monkeypatch):
    queries = []

    async def geocode(query, country=None):
        queries.append(query)
        return GeoPoint(latitude=39.2073, longitude=-76.7147, postal_code="21075")
    monkeypatch.setattr(kitchen_admin, "geocode_address", geocode)
    return queries


def test_missing_fields_are_named():
    with pytest.raises(kitchen_admin.KitchenFormError, match="City"):
        kitchen_admin.parse_form({**FORM, "city": ""})


def test_half_a_coordinate_is_refused():
    with pytest.raises(kitchen_admin.KitchenFormError, match="both"):
        kitchen_admin.parse_form({**FORM, "latitude": "39.2"})


async def test_a_new_kitchen_is_found_on_the_map_and_stored_in_km(session, geocoded):
    kitchen = await kitchen_admin.save(session, None, kitchen_admin.parse_form(FORM))

    assert geocoded == ["6639 Cambria Terrace, Elkridge, MD 21075"]
    assert (kitchen.latitude, kitchen.longitude) == (39.2073, -76.7147)
    assert kitchen.delivery_radius_km == pytest.approx(16.093, abs=0.001)
    assert kitchen.code == "SHERO-ELKRIDGE" and kitchen.is_primary and kitchen.is_active
    assert kitchen.operating_hours == {"mon": [["09:00", "21:00"]]}


async def test_typed_coordinates_win_over_the_address(session, geocoded, outlet):
    data = kitchen_admin.parse_form({**FORM, "latitude": "40.1", "longitude": "-74.2"})
    kitchen = await kitchen_admin.save(session, None, data)
    assert geocoded == [] and kitchen.latitude == 40.1
    # A second kitchen is not the default, and gets its own code.
    assert kitchen.is_primary is False


async def test_an_address_the_map_cannot_find_is_reported(session, monkeypatch):
    async def nowhere(query, country=None):
        return None
    monkeypatch.setattr(kitchen_admin, "geocode_address", nowhere)
    with pytest.raises(kitchen_admin.KitchenFormError, match="map"):
        await kitchen_admin.save(session, None, kitchen_admin.parse_form(FORM))


async def test_codes_stay_unique(session, geocoded):
    first = await kitchen_admin.save(session, None, kitchen_admin.parse_form(FORM))
    second = await kitchen_admin.save(session, None, kitchen_admin.parse_form(FORM))
    assert first.code != second.code


async def test_make_primary_moves_the_default(session, outlet, far_outlet):
    await kitchen_admin.make_primary(session, far_outlet)
    assert far_outlet.is_primary and not outlet.is_primary
