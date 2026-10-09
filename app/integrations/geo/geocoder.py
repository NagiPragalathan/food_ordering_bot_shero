"""Turn a postal code into coordinates, and a map pin back into an address.

A WhatsApp location pin already carries lat/lng, so this is only needed when
the customer types a ZIP instead (spec steps 6a and 9).

Two backends: Google Geocoding when GOOGLE_MAPS_API_KEY is set, otherwise
OpenStreetMap Nominatim, which is free but rate-limited to roughly one call a
second and requires an identifying User-Agent. Results are cached for the
process lifetime because a postal code's centroid does not move.

The reverse lookup fills the web page's address form from a dropped pin or
the phone's own location. It only pre-fills: the customer can still correct
the street before saving.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "SheroOrderingBot/1.0 (+https://shero.example)"


@dataclass(frozen=True)
class GeoPoint:
    latitude: float
    longitude: float
    formatted_address: str = ""
    postal_code: str = ""
    city: str = ""
    state: str = ""
    source: str = ""
    # Street and house number only, e.g. "12 Maple Street". Set by the
    # reverse lookup; a ZIP lookup has no street to give.
    street: str = ""
    # The state's short form ("MD") where the source gives one; Google does.
    state_code: str = ""


_cache: dict[str, GeoPoint | None] = {}


async def geocode_postal_code(postal_code: str,
                              country: str | None = None) -> GeoPoint | None:
    """Resolve a postal code, or None if it cannot be resolved.

    Returning None (rather than raising) is deliberate: the caller re-asks the
    customer for their location, which is the spec's "Re-ask if location can't
    be read" path.
    """
    code = (postal_code or "").strip().upper()
    if not code:
        return None

    country = country or settings.geocoder_country
    cache_key = f"{country}:{code}"
    if cache_key in _cache:
        return _cache[cache_key]

    try:
        if settings.google_maps_api_key:
            point = await _google_geocode(code, country)
        else:
            point = await _nominatim_geocode(code, country)
    except httpx.HTTPError as exc:
        # A geocoder outage must not crash the conversation; the customer is
        # asked to share a location pin instead.
        log.warning("geocode_failed", postal_code=code, error=str(exc))
        return None

    _cache[cache_key] = point
    if point is None:
        log.info("geocode_no_match", postal_code=code, country=country)
    return point


async def _google_geocode(code: str, country: str) -> GeoPoint | None:
    params = {
        "address": code,
        "components": f"country:{country}",
        "key": settings.google_maps_api_key,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(GOOGLE_GEOCODE_URL, params=params)
    response.raise_for_status()
    payload = response.json()

    if payload.get("status") != "OK" or not payload.get("results"):
        if payload.get("status") not in ("ZERO_RESULTS", "OK"):
            log.warning("google_geocode_status", status=payload.get("status"),
                        message=payload.get("error_message"))
        return None

    result = payload["results"][0]
    location = result["geometry"]["location"]
    components = {
        comp_type: comp.get("short_name", "")
        for comp in result.get("address_components", [])
        for comp_type in comp.get("types", [])
    }
    return GeoPoint(
        latitude=float(location["lat"]),
        longitude=float(location["lng"]),
        formatted_address=result.get("formatted_address", ""),
        postal_code=components.get("postal_code", code),
        city=components.get("locality", "") or components.get("sublocality", ""),
        state=components.get("administrative_area_level_1", ""),
        state_code=short.get("administrative_area_level_1", ""),
        source="google",
    )


async def _nominatim_geocode(code: str, country: str) -> GeoPoint | None:
    params = {
        "postalcode": code,
        "country": country,
        "format": "json",
        "limit": 1,
        "addressdetails": 1,
    }
    async with httpx.AsyncClient(timeout=10.0,
                                 headers={"User-Agent": USER_AGENT}) as client:
        response = await client.get(NOMINATIM_URL, params=params)
    response.raise_for_status()
    results = response.json()
    if not results:
        return None

    result = results[0]
    address = result.get("address", {})
    return GeoPoint(
        latitude=float(result["lat"]),
        longitude=float(result["lon"]),
        formatted_address=result.get("display_name", ""),
        postal_code=address.get("postcode", code),
        city=address.get("city") or address.get("town") or address.get("village", ""),
        state=address.get("state", ""),
        source="nominatim",
    )


# ~11 m of latitude. Pins closer than this share a street address, so nudging
# the map a few pixels does not cost another call to a rate-limited service.
REVERSE_PRECISION = 4
_reverse_cache: dict[tuple[float, float], GeoPoint | None] = {}


async def reverse_geocode(latitude: float, longitude: float) -> GeoPoint | None:
    """The street address at a point, or None if nothing sensible is there.

    None rather than an exception, like the forward lookup: the page then
    leaves the fields for the customer to type, which is exactly what it did
    before there was a map.
    """
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None

    key = (round(latitude, REVERSE_PRECISION), round(longitude, REVERSE_PRECISION))
    if key in _reverse_cache:
        return _reverse_cache[key]

    try:
        if settings.google_maps_api_key:
            point = await _google_reverse(latitude, longitude)
        else:
            point = await _nominatim_reverse(latitude, longitude)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        log.warning("reverse_geocode_failed", error=str(exc))
        return None

    _reverse_cache[key] = point
    return point


async def _google_reverse(latitude: float, longitude: float) -> GeoPoint | None:
    # result_type narrows the answer to a real street address: without it
    # Google may lead with a road or a neighbourhood, which has no house number.
    params = {"latlng": f"{latitude},{longitude}", "key": settings.google_maps_api_key,
              "result_type": "street_address|premise|subpremise"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(GOOGLE_GEOCODE_URL, params=params)
        response.raise_for_status()
        point = _google_point(response.json(), at=(latitude, longitude))
        if point is None:
            # Open ground or a park: take the nearest anything instead.
            params.pop("result_type")
            response = await client.get(GOOGLE_GEOCODE_URL, params=params)
            response.raise_for_status()
            point = _google_point(response.json(), at=(latitude, longitude))
    return point


def _google_point(payload: dict, *, at: tuple[float, float] | None = None
                  ) -> GeoPoint | None:
    """First Geocoding API result as a GeoPoint with its street line.

    `at` keeps the customer's own pin as the point: the door they dropped it
    on is a better place to send a driver than Google's address centroid.
    """
    if payload.get("status") != "OK" or not payload.get("results"):
        if payload.get("status") not in ("ZERO_RESULTS", "OK"):
            log.warning("google_geocode_status", status=payload.get("status"),
                        message=payload.get("error_message"))
        return None

    result = payload["results"][0]
    components = {
        comp_type: comp.get("long_name", "")
        for comp in result.get("address_components", [])
        for comp_type in comp.get("types", [])
    }
    short = {
        comp_type: comp.get("short_name", "")
        for comp in result.get("address_components", [])
        for comp_type in comp.get("types", [])
    }
    street = " ".join(part for part in (components.get("street_number", ""),
                                        components.get("route", "")) if part)
    location = result["geometry"]["location"]
    latitude, longitude = at or (float(location["lat"]), float(location["lng"]))
    return GeoPoint(
        latitude=latitude,
        longitude=longitude,
        formatted_address=result.get("formatted_address", ""),
        postal_code=components.get("postal_code", ""),
        city=components.get("locality", "") or components.get("sublocality", ""),
        state=components.get("administrative_area_level_1", ""),
        state_code=short.get("administrative_area_level_1", ""),
        source="google",
        street=street,
    )


async def _nominatim_reverse(latitude: float, longitude: float) -> GeoPoint | None:
    params = {"lat": latitude, "lon": longitude, "format": "json",
              "addressdetails": 1, "zoom": 18}
    async with httpx.AsyncClient(timeout=10.0,
                                 headers={"User-Agent": USER_AGENT}) as client:
        response = await client.get(NOMINATIM_REVERSE_URL, params=params)
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict) or "error" in result:
        return None
    return _nominatim_point(result, latitude, longitude)


def _nominatim_point(result: dict, latitude: float, longitude: float) -> GeoPoint:
    address = result.get("address", {})
    street = " ".join(part for part in (address.get("house_number", ""),
                                        address.get("road", "")) if part)
    return GeoPoint(
        latitude=latitude,
        longitude=longitude,
        formatted_address=result.get("display_name", ""),
        postal_code=address.get("postcode", ""),
        city=address.get("city") or address.get("town") or address.get("village", ""),
        state=address.get("state", ""),
        source="nominatim",
        street=street,
    )


async def geocode_address(text: str, country: str | None = None) -> GeoPoint | None:
    """A typed address ("12 Maple St, Edison") to a point with its street.

    Backs the search box above the map on the ordering page. Google resolves
    US house numbers far more reliably than OpenStreetMap, so set a key.
    """
    query = " ".join((text or "").split())[:200]
    if len(query) < 3:
        return None
    country = country or settings.geocoder_country

    try:
        if settings.google_maps_api_key:
            return await _google_address(query, country)
        return await _nominatim_address(query, country)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        log.warning("address_search_failed", error=str(exc))
        return None


async def _google_address(query: str, country: str) -> GeoPoint | None:
    params = {"address": query, "components": f"country:{country}",
              "key": settings.google_maps_api_key}
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(GOOGLE_GEOCODE_URL, params=params)
    response.raise_for_status()
    return _google_point(response.json())


async def _nominatim_address(query: str, country: str) -> GeoPoint | None:
    params = {"q": query, "countrycodes": country.lower(), "format": "json",
              "limit": 1, "addressdetails": 1}
    async with httpx.AsyncClient(timeout=10.0,
                                 headers={"User-Agent": USER_AGENT}) as client:
        response = await client.get(NOMINATIM_URL, params=params)
    response.raise_for_status()
    results = response.json()
    if not results:
        return None
    return _nominatim_point(results[0], float(results[0]["lat"]),
                            float(results[0]["lon"]))


def clear_cache() -> None:
    _cache.clear()
    _reverse_cache.clear()
