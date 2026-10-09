"""The kitchen form's map picker: a dropped pin gives an address, a searched
address gives a pin (admin/routes/kitchens.geo_lookup)."""

from __future__ import annotations

import json

from app.admin.routes import kitchens
from app.integrations.geo import geocoder
from app.integrations.geo.geocoder import GeoPoint

ELKRIDGE = GeoPoint(latitude=39.212601, longitude=-76.723301,
                    formatted_address="6021 University Blvd, Elkridge, MD 21075, USA",
                    postal_code="21075", city="Elkridge", state="Maryland",
                    state_code="MD", street="6021 University Blvd", source="google")


async def test_a_dropped_pin_fills_the_address_and_keeps_its_own_spot(monkeypatch):
    async def reverse(lat, lng):
        return ELKRIDGE
    monkeypatch.setattr(kitchens, "reverse_geocode", reverse)

    result = await kitchens.geo_lookup(lat=39.2130004, lng=-76.7240004, current_user=None)

    assert result == {"found": True, "lat": 39.213, "lng": -76.724,
                      "street": "6021 University Blvd", "city": "Elkridge",
                      "state": "MD", "zip": "21075",
                      "label": "6021 University Blvd, Elkridge, MD 21075, USA"}


async def test_a_searched_address_gives_its_pin(monkeypatch):
    seen = []

    async def search(text):
        seen.append(text)
        return ELKRIDGE
    monkeypatch.setattr(kitchens, "geocode_address", search)

    result = await kitchens.geo_lookup(q="  6021 University Blvd  ", current_user=None)

    assert seen == ["6021 University Blvd"]
    assert (result["lat"], result["lng"]) == (39.212601, -76.723301)


async def test_nothing_found_says_so(monkeypatch):
    async def search(text):
        return None
    monkeypatch.setattr(kitchens, "geocode_address", search)

    result = await kitchens.geo_lookup(q="nowhere at all", current_user=None)
    assert result == {"found": False, "error": "Could not find that on the map."}


async def test_a_failing_geocoder_does_not_break_the_form(monkeypatch):
    async def broken(text):
        raise RuntimeError("google down")
    monkeypatch.setattr(kitchens, "geocode_address", broken)

    response = await kitchens.geo_lookup(q="6021 University Blvd", current_user=None)
    assert response.status_code == 502
    assert json.loads(response.body)["found"] is False


async def test_an_empty_lookup_is_refused():
    response = await kitchens.geo_lookup(current_user=None)
    assert response.status_code == 400


def test_google_gives_the_state_short_form():
    payload = {"status": "OK", "results": [{
        "formatted_address": "6021 University Blvd, Elkridge, MD 21075, USA",
        "geometry": {"location": {"lat": 39.21, "lng": -76.72}},
        "address_components": [
            {"long_name": "6021", "short_name": "6021", "types": ["street_number"]},
            {"long_name": "University Boulevard", "short_name": "University Blvd",
             "types": ["route"]},
            {"long_name": "Maryland", "short_name": "MD",
             "types": ["administrative_area_level_1", "political"]},
        ]}]}
    point = geocoder._google_point(payload)
    assert (point.state, point.state_code) == ("Maryland", "MD")
