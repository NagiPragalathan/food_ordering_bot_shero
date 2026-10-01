"""Saving kitchens from the admin Kitchens page.

The route stays thin: it hands the posted form here. This module parses it,
finds the kitchen's map point (typed coordinates win; otherwise the address
is geocoded), and applies it to the Outlet row. Parsing is pure so it can be
tested without a request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Outlet
from app.integrations.geo.geocoder import geocode_address
from app.services import kitchen as kitchen_service
from app.services.slots import WEEKDAY_KEYS

log = get_logger(__name__)

DEFAULT_RADIUS_MILES = 10.0
AREA_MODES = ("radius", "zips", "both")


class KitchenFormError(ValueError):
    """A problem with the posted kitchen form, worded for the admin."""


@dataclass
class KitchenForm:
    name: str
    address_line1: str
    city: str
    state: str
    postal_code: str
    latitude: float | None
    longitude: float | None
    service_area_mode: str
    radius_miles: float
    service_zips: list[str]
    kitchen_whatsapp: str | None
    phone: str | None
    timezone: str
    operating_hours: dict
    slot_length_minutes: int
    slot_capacity: int
    is_active: bool


def parse_form(form) -> KitchenForm:
    """Read and check the posted kitchen form."""
    def text(key: str) -> str:
        return str(form.get(key) or "").strip()

    required = {"name": "Kitchen name", "address_line1": "Street address",
                "city": "City", "state": "State", "postal_code": "ZIP"}
    missing = [label for key, label in required.items() if not text(key)]
    if missing:
        raise KitchenFormError(f"Fill in: {', '.join(missing)}.")

    latitude, longitude = _number(text("latitude")), _number(text("longitude"))
    if (latitude is None) != (longitude is None):
        raise KitchenFormError("Enter both latitude and longitude, or leave both blank.")
    if (latitude is not None and longitude is not None
            and not (-90 <= latitude <= 90 and -180 <= longitude <= 180)):
        raise KitchenFormError("Latitude or longitude is out of range.")

    radius = _number(text("radius_miles"))
    if radius is None or radius <= 0:
        radius = DEFAULT_RADIUS_MILES
    mode = text("service_area_mode").lower()

    return KitchenForm(
        name=text("name")[:120],
        address_line1=text("address_line1")[:255],
        city=text("city")[:120],
        state=text("state")[:80],
        postal_code=text("postal_code")[:20],
        latitude=latitude,
        longitude=longitude,
        service_area_mode=mode if mode in AREA_MODES else "radius",
        radius_miles=radius,
        service_zips=parse_zips(text("service_zips")),
        kitchen_whatsapp=text("kitchen_whatsapp") or None,
        phone=text("kitchen_phone") or None,
        timezone=text("timezone_name") or "America/New_York",
        operating_hours=parse_hours(form),
        slot_length_minutes=max(15, min(240, int(_number(text("slot_length_minutes")) or 60))),
        slot_capacity=max(1, int(_number(text("slot_capacity")) or 4)),
        is_active=bool(form.get("is_active")),
    )


async def locate(data: KitchenForm) -> tuple[float, float]:
    """The kitchen's map point: typed coordinates, else the geocoded address.

    Distances to customers are measured from this point, so a kitchen is
    never saved without one.
    """
    if data.latitude is not None and data.longitude is not None:
        return data.latitude, data.longitude
    query = f"{data.address_line1}, {data.city}, {data.state} {data.postal_code}"
    point = await geocode_address(query)
    if point is None:
        raise KitchenFormError(
            "Could not find that address on the map. Check it, or paste the "
            "latitude and longitude from Google Maps.")
    log.info("kitchen_geocoded", query=query, latitude=point.latitude,
             longitude=point.longitude)
    return point.latitude, point.longitude


async def save(session: AsyncSession, kitchen: Outlet | None, data: KitchenForm) -> Outlet:
    """Create the kitchen (kitchen=None) or update it from the form."""
    latitude, longitude = await locate(data)
    if kitchen is None:
        kitchen = Outlet(code=await unique_code(session, data.name), name=data.name,
                         address_line1=data.address_line1, city=data.city,
                         state=data.state, postal_code=data.postal_code,
                         latitude=latitude, longitude=longitude,
                         # The first kitchen is the default one.
                         is_primary=not await _any_kitchen(session))
        session.add(kitchen)

    kitchen.name = data.name
    kitchen.address_line1 = data.address_line1
    kitchen.city = data.city
    kitchen.state = data.state
    kitchen.postal_code = data.postal_code
    kitchen.latitude, kitchen.longitude = latitude, longitude
    kitchen.service_area_mode = data.service_area_mode
    kitchen.delivery_radius_km = kitchen_service.km(data.radius_miles)
    kitchen.service_zips = data.service_zips
    kitchen.kitchen_whatsapp = data.kitchen_whatsapp
    kitchen.phone = data.phone
    kitchen.timezone = data.timezone
    kitchen.operating_hours = data.operating_hours
    kitchen.slot_length_minutes = data.slot_length_minutes
    kitchen.slot_capacity = data.slot_capacity
    kitchen.is_active = data.is_active
    await session.flush()
    return kitchen


async def make_primary(session: AsyncSession, kitchen: Outlet) -> None:
    """The default kitchen, used where no address has been checked yet."""
    for other in (await session.execute(select(Outlet))).scalars():
        other.is_primary = other.id == kitchen.id
    await session.flush()


async def unique_code(session: AsyncSession, name: str) -> str:
    """An Outlet Code from the name (also the Zoho Vendor key), made unique."""
    base = re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-")[:32] or "KITCHEN"
    taken = set((await session.execute(select(Outlet.code))).scalars())
    code, n = base, 2
    while code in taken:
        code, n = f"{base[:30]}-{n}", n + 1
    return code


async def _any_kitchen(session: AsyncSession) -> bool:
    return (await session.execute(select(Outlet.id).limit(1))).first() is not None


# --- field parsing -----------------------------------------------------------
def parse_hours(form) -> dict[str, list[list[str]]]:
    """Build the `operating_hours` blob from the seven open/close pairs.

    Shape matches what `slots._windows_for_day` reads:
    `{"mon": [["11:00", "21:00"]], ...}`. A day with either field blank is
    treated as closed and simply left out, which is how the kitchen says
    "we do not deliver on Sundays".

    A close time at or before the open time is kept as-is: slot generation
    reads that as trading past midnight, which is a real case for a kitchen
    open 18:00-01:00.
    """
    hours: dict[str, list[list[str]]] = {}
    for key in WEEKDAY_KEYS:
        opens = clean_time(form.get(f"hours_{key}_open"))
        closes = clean_time(form.get(f"hours_{key}_close"))
        if opens is None or closes is None:
            if opens != closes:  # one side filled in, the other not
                log.warning("kitchen_hours_ignored", day=key,
                            opens=form.get(f"hours_{key}_open"),
                            closes=form.get(f"hours_{key}_close"))
            continue
        hours[key] = [[opens, closes]]
    return hours


def clean_time(raw) -> str | None:
    """Normalise one time field to `HH:MM`, or None if it is not a time.

    Browsers post `<input type="time">` as HH:MM, but some send HH:MM:SS, and
    the endpoint is reachable by anything, so canonicalise here rather than
    store whatever arrived in the blob slot generation re-parses.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).time().strftime("%H:%M")
        except ValueError:
            continue
    return None


def parse_zips(raw: str) -> list[str]:
    """Split a comma/newline separated ZIP list into normalised codes."""
    parts = raw.replace("\n", ",").replace(";", ",").split(",")
    seen: dict[str, None] = {}
    for part in parts:
        code = "".join(part.upper().split()).replace("-", "")
        if code:
            seen.setdefault(code, None)
    return list(seen)


def _number(text: str) -> float | None:
    try:
        return float(text) if text else None
    except ValueError:
        return None
