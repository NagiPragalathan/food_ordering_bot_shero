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


async def test_a_zip_code_gives_the_state_and_its_short_form(google):
    # This lookup used to raise NameError on every Google answer.
    answers, _ = google
    answers.append({"status": "OK", "results": [{
        "formatted_address": "Ellicott City, MD 21043, USA",
        "geometry": {"location": {"lat": 39.27, "lng": -76.83}},
        "address_components": [
            {"long_name": "21043", "short_name": "21043", "types": ["postal_code"]},
            {"long_name": "Ellicott City", "short_name": "Ellicott City", "types": ["locality"]},
            {"long_name": "Maryland", "short_name": "MD",
             "types": ["administrative_area_level_1", "political"]},
        ],
    }]})

    point = await geocoder.geocode_postal_code("21043")

    assert (point.postal_code, point.city, point.state_code) == ("21043", "Ellicott City", "MD")
    assert point.source == "google"


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


# --- India: the address at the pin, with its area ------------------------------------
def _in(formatted, lat, lng, types, components, location_type="ROOFTOP"):
    return {"formatted_address": formatted, "types": types,
            "geometry": {"location": {"lat": lat, "lng": lng}, "location_type": location_type},
            "address_components": components}


AYANAVARAM = [
    {"long_name": "Ayanavaram", "short_name": "Ayanavaram", "types": ["sublocality"]},
    {"long_name": "Chennai", "short_name": "Chennai", "types": ["locality"]},
    {"long_name": "Tamil Nadu", "short_name": "TN", "types": ["administrative_area_level_1"]},
    {"long_name": "India", "short_name": "IN", "types": ["country"]},
    {"long_name": "600023", "short_name": "600023", "types": ["postal_code"]},
]
PIN = (13.0990, 80.2335)


async def test_an_indian_pin_fills_door_street_area_and_city(google):
    answers, _ = google
    answers.append({"status": "OK", "results": [
        _in("108/49, Vasantha nagar, Chinna Chembarambakkam, Ayanavaram, Chennai, Tamil Nadu 600023, India",
            13.09909, 80.23352, ["street_address", "subpremise"],
            [{"long_name": "108/49", "short_name": "108/49", "types": ["subpremise"]}] + AYANAVARAM)]})
    point = await geocoder.reverse_geocode(*PIN)
    # No road name in Google's answer: the old street line was empty here.
    assert point.address_line == "108/49, Vasantha nagar, Chinna Chembarambakkam, Ayanavaram, Chennai"
    assert point.postal_code == "600023"


async def test_a_house_far_from_the_pin_is_not_taken_the_road_at_the_pin_is(google):
    answers, calls = google
    far_house = _in("14/78, Clemens Road, Purasaiwakkam, Chennai, Tamil Nadu 600007, India",
                    13.0870, 80.2520, ["street_address"],
                    [{"long_name": "14/78", "short_name": "14/78", "types": ["street_number"]},
                     {"long_name": "Clemens Road", "short_name": "Clemens Rd", "types": ["route"]}]
                    + AYANAVARAM[1:4] + [{"long_name": "600007", "short_name": "600007",
                                          "types": ["postal_code"]}])
    road = _in("Konnur High Rd, Ayanavaram, Chennai, Tamil Nadu, India", 13.0991, 80.2338, ["route"],
               [{"long_name": "Konnur High Road", "short_name": "Konnur High Rd", "types": ["route"]}]
               + AYANAVARAM[:4], location_type="GEOMETRIC_CENTER")
    plus = _in("36XM+J9 Chennai, Tamil Nadu, India", *PIN, ["plus_code"], AYANAVARAM[1:4])
    answers.extend([{"status": "OK", "results": [far_house]},
                    {"status": "OK", "results": [plus, road]}])
    point = await geocoder.reverse_geocode(*PIN)
    assert "Clemens" not in point.address_line                 # ~2 km away: another house
    assert point.address_line == "Konnur High Rd, Ayanavaram, Chennai"
    assert len(calls) == 2


async def test_a_bracket_left_open_in_googles_data_is_closed(google):
    answers, _ = google
    answers.append({"status": "OK", "results": [
        _in("50(1, EVK Sampath Salai, Vepery, Chennai, Tamil Nadu 600007, India", 13.0838, 80.2622,
            ["street_address"],
            [{"long_name": "50(1", "short_name": "50(1", "types": ["street_number"]},
             {"long_name": "EVK Sampath Salai", "short_name": "EVK Sampath Salai", "types": ["route"]},
             {"long_name": "Tamil Nadu", "short_name": "TN", "types": ["administrative_area_level_1"]},
             {"long_name": "India", "short_name": "IN", "types": ["country"]},
             {"long_name": "600007", "short_name": "600007", "types": ["postal_code"]}])]})
    point = await geocoder.reverse_geocode(13.0838, 80.2622)
    assert point.address_line == "50(1), EVK Sampath Salai, Vepery, Chennai"
    assert point.street == "50(1) EVK Sampath Salai"


async def test_a_us_pin_still_fills_only_number_and_street(google):
    answers, _ = google
    answers.append(MAPLE)
    point = await geocoder.reverse_geocode(40.52015, -74.41023)
    assert point.address_line == point.street == "12 Maple Street"
