"""The customer's delivery address: where it really is, and how Uber reads it.

`locate` decides the point every distance check measures. The typed street
and ZIP are what Uber delivers to, so they decide; a map pin only sharpens
them. SHO-261108-7QW2Q: the ordering page's map opens on the kitchen, the pin
was left there, and the customer typed an address in Washington DC - our check
measured the pin (6.4 km) and let it through, while Uber measured the address
(23.71 miles) and refused it after the customer had paid.

`uber_dropoff` is one builder for the quote at checkout and the booking before
the slot, so both send the same, complete address: the street and unit, the
ZIP, and the ZIP's own city and state. Nothing is filled in by guesswork - a
customer's street with the kitchen's town is another place - so an address
whose town cannot be found is refused and the customer asked to check it.
"""

from __future__ import annotations

from app.core.exceptions import BotFlowError
from app.core.logging import get_logger
from app.db.models import Outlet
from app.integrations.geo.distance import haversine_km
from app.integrations.geo.geocoder import GeoPoint, geocode_address, geocode_postal_code
from app.integrations.uber import direct as uber

log = get_logger(__name__)

# A pin this close to where the typed address is found is the same place (the
# far side of a big building or a long drive). Further away, it is elsewhere.
PIN_AT_ADDRESS_KM = 1.5
# When only the ZIP could be found, a pin this far from the ZIP's centre is
# not in that ZIP.
PIN_IN_ZIP_KM = 10.0


async def locate(*, street: str | None, postal_code: str | None,
                 latitude: float | None, longitude: float | None) -> GeoPoint | None:
    """The point the delivery goes to, for the kitchen distance check.

    The pin is kept when it sits at the typed address, so a driver still gets
    the exact door. A pin somewhere else is set aside for the typed address
    (or, if the street cannot be found, its ZIP). With nothing typed to check
    against, the pin stands. None when there is neither.
    """
    zip_code = (postal_code or "").strip()
    pin = None
    if latitude is not None and longitude is not None:
        pin = GeoPoint(latitude=float(latitude), longitude=float(longitude),
                       postal_code=zip_code, source="pin")

    reference, tolerance = await _typed_point(street, zip_code), PIN_AT_ADDRESS_KM
    if reference is None and zip_code:
        reference, tolerance = await geocode_postal_code(zip_code), PIN_IN_ZIP_KM
    if reference is None:
        return pin
    if pin is None:
        return reference

    gap = haversine_km(pin.latitude, pin.longitude, reference.latitude, reference.longitude)
    if gap <= tolerance:
        return pin
    log.warning("pin_not_at_address", gap_km=round(gap, 1), zip=zip_code or None,
                measured_from="street" if tolerance == PIN_AT_ADDRESS_KM else "zip")
    return reference


async def _typed_point(street: str | None, zip_code: str) -> GeoPoint | None:
    """The typed street found inside its ZIP, or None when the geocoder only
    matched something vaguer (no house number) or somewhere else (another ZIP)."""
    street = " ".join((street or "").split())
    if not street or not zip_code:
        return None
    point = await geocode_address(f"{street}, {zip_code}")
    if point is None or not point.street[:1].isdigit():
        return None
    if point.postal_code and point.postal_code[:5] != zip_code[:5]:
        return None
    return point


class AddressNotFound(BotFlowError):
    """The address's own city and state could not be found. Nothing is
    guessed in their place: the customer is asked to check the address."""


ADDRESS_NOT_FOUND = ("We couldn't find that address on the map. Please check the street "
                     "and ZIP code, then try again.")


async def uber_dropoff(*, street: str | None, unit: str | None, postal_code: str | None,
                       kitchen: Outlet) -> dict:
    """Uber's address dict for a customer's delivery address.

    Raises AddressNotFound when the address's own city and state cannot be
    found - never the kitchen's town in their place.
    """
    street, zip_code = (street or "").strip(), (postal_code or "").strip()
    city, state = await town(street, zip_code)
    if not (street and zip_code and city and state):
        raise AddressNotFound(f"no city/state for {street!r}, ZIP {zip_code!r}",
                              customer_message=ADDRESS_NOT_FOUND)
    address = uber.build_address(street=street, city=city, state=state, zip_code=zip_code,
                                 country=kitchen.country or "US")
    if unit and unit.strip():
        address["street_address"].append(unit.strip())
    return address


async def town(street: str, zip_code: str) -> tuple[str, str]:
    """The address's own city and state (short form where known): the ZIP's,
    else the typed street's. Both lookups are cached by the geocoder, so a
    quote and the confirm that follows ask once."""
    if not zip_code:
        return "", ""
    for lookup in (lambda: geocode_postal_code(zip_code),
                   lambda: geocode_address(f"{street}, {zip_code}") if street else _none()):
        point = await lookup()
        if point is not None and point.city and (point.state_code or point.state):
            return point.city, point.state_code or point.state
    return "", ""


async def _none() -> None:
    return None
