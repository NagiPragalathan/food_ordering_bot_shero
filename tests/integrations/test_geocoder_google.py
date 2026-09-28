"""Google Geocoding: map pin -> street, and typed address -> point.

Google is faked at the HTTP layer, so these check how its answers are read,
not the network.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.config import settings
from app.integrations.geo import geocoder

MAPLE = {
    "status": "OK",
    "results": [{
        "formatted_address": "12 Maple St, Edison, NJ 08820, USA",
        "geometry": {"location": {"lat": 40.5201, "lng": -74.4102}},
        "address_components": [
            {"long_name": "12", "types": ["street_number"]},
            {"long_name": "Maple Street", "types": ["route"]},
            {"long_name": "Edison", "types": ["locality", "political"]},
            {"long_name": "New Jersey", "types": ["administrative_area_level_1"]},
            {"long_name": "08820", "types": ["postal_code"]},
        ],
    }],
}
EMPTY = {"status": "ZERO_RESULTS", "results": []}


@pytest.fixture
def google(monkeypatch):
    """Route Geocoding API calls to a script of canned answers."""
    monkeypatch.setattr(settings, "google_maps_api_key", "test-key")
    geocoder.clear_cache()
    calls: list[dict] = []
    answers: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        return httpx.Response(200, json=answers.pop(0) if answers else EMPTY)

    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(geocoder.httpx, "AsyncClient", client)
    yield answers, calls
    geocoder.clear_cache()


async def test_a_pin_becomes_a_street_with_house_number(google):
    answers, calls = google
    answers.append(MAPLE)

    point = await geocoder.reverse_geocode(40.52015, -74.41023)

    assert point.street == "12 Maple Street"
    assert point.postal_code == "08820"
    assert point.source == "google"
    # The customer's own pin is kept, not Google's address centroid.
    assert (point.latitude, point.longitude) == (40.52015, -74.41023)
    assert "street_address" in calls[0]["result_type"]


async def test_open_ground_falls_back_to_the_nearest_anything(google):
    answers, calls = google
    answers.extend([EMPTY, MAPLE])

    point = await geocoder.reverse_geocode(40.52, -74.41)

    assert point.street == "12 Maple Street"
    assert len(calls) == 2
    assert "result_type" not in calls[1]


async def test_a_typed_address_is_searched_in_the_configured_country(google):
    answers, calls = google
    answers.append(MAPLE)

    point = await geocoder.geocode_address("12 maple st edison")

    assert (point.latitude, point.longitude) == (40.5201, -74.4102)
    assert point.street == "12 Maple Street"
    assert calls[0]["components"] == f"country:{settings.geocoder_country}"


async def test_a_search_with_no_match_is_none(google):
    assert await geocoder.geocode_address("zzzz nowhere") is None


async def test_a_too_short_search_does_not_call_google(google):
    _, calls = google
    assert await geocoder.geocode_address("ab") is None
    assert calls == []


async def test_a_google_outage_is_none_not_a_crash(monkeypatch):
    monkeypatch.setattr(settings, "google_maps_api_key", "test-key")
    geocoder.clear_cache()
    real = httpx.AsyncClient

    def broken(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda r: httpx.Response(503))
        return real(*args, **kwargs)

    monkeypatch.setattr(geocoder.httpx, "AsyncClient", broken)
    assert await geocoder.reverse_geocode(40.5, -74.4) is None
    assert await geocoder.geocode_address("12 Maple St") is None
