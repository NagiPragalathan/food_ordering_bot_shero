"""Turn a postal code into coordinates.

A WhatsApp location pin already carries lat/lng, so this is only needed when
the customer types a ZIP instead (spec steps 6a and 9).

Two backends: Google Geocoding when GOOGLE_MAPS_API_KEY is set, otherwise
OpenStreetMap Nominatim, which is free but rate-limited to roughly one call a
second and requires an identifying User-Agent. Results are cached for the
process lifetime because a postal code's centroid does not move.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
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


def clear_cache() -> None:
    _cache.clear()
