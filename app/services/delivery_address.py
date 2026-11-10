"""The customer's address as Uber reads it - one builder for the quote at
checkout and the booking before the slot.

Uber places a delivery by its address first (the coordinates only add
precision), so both calls must send the same, complete address: the street
and unit, the ZIP, and the ZIP's own city and state. The kitchen's city is
only a last resort - a customer's street with the kitchen's town is another
place, which Uber priced or refused as one (an address 4 miles away came back
as 23.7, beyond its 10-mile limit).
"""

from __future__ import annotations

from app.db.models import Outlet
from app.integrations.geo.geocoder import geocode_postal_code
from app.integrations.uber import direct as uber


async def uber_dropoff(*, street: str | None, unit: str | None, postal_code: str | None,
                       kitchen: Outlet) -> dict:
    """Uber's address dict for a customer's delivery address."""
    zip_code = (postal_code or "").strip()
    city, state = await _city_and_state(zip_code)
    address = uber.build_address(street=(street or "").strip(), city=city or kitchen.city,
                                 state=state or kitchen.state, zip_code=zip_code,
                                 country=kitchen.country or "US")
    if unit and unit.strip():
        address["street_address"].append(unit.strip())
    return address


async def _city_and_state(zip_code: str) -> tuple[str, str]:
    """The ZIP's city and state (short form where known). Cached by the
    geocoder, so a quote and the confirm that follows look it up once."""
    if not zip_code:
        return "", ""
    point = await geocode_postal_code(zip_code)
    if point is None:
        return "", ""
    return point.city or "", point.state_code or point.state or ""
