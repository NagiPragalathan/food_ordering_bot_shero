"""Kitchens and their delivery areas (spec steps 6a, 9, 10).

Any number of kitchens can be set up on the admin Kitchens page. An address
is served when at least one active kitchen covers it, and the order is then
cooked at the nearest kitchen that does: that kitchen is stored on the
customer (preferred_outlet_id) when the address is checked, and copied onto
the order, so its slots, its Uber pickup and its alert all follow.

Each kitchen's area is decided by its `service_area_mode`:

    radius  distance <= delivery_radius_km (the admin enters miles)
    zips    the postal code is in the kitchen's list
    both    inside the radius AND in the list
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Customer, Outlet
from app.integrations.geo.distance import haversine_km, round_km
from app.integrations.geo.geocoder import GeoPoint, geocode_postal_code

log = get_logger(__name__)

KM_PER_MILE = 1.609344


@dataclass(frozen=True)
class ServiceCheck:
    is_serviceable: bool
    distance_km: float | None
    reason: str
    kitchen_name: str = ""
    # The kitchen that will cook the order: the nearest one covering the
    # address. None when nothing covers it.
    kitchen: Outlet | None = field(default=None, compare=False, repr=False)

    @property
    def distance_display(self) -> str:
        return f"{self.distance_km} km" if self.distance_km is not None else ""


async def active_kitchens(session: AsyncSession) -> list[Outlet]:
    """Every kitchen taking orders, primary first, then oldest."""
    result = await session.execute(
        select(Outlet)
        .where(Outlet.is_active.is_(True))
        .order_by(Outlet.is_primary.desc(), Outlet.created_at)
    )
    return list(result.scalars())


async def get_kitchen(session: AsyncSession) -> Outlet | None:
    """The default kitchen: the primary one, else the oldest active one.

    Used only where no address has been checked yet (the menu page header,
    an old conversation); orders use the kitchen picked for the address.
    """
    kitchens = await active_kitchens(session)
    return kitchens[0] if kitchens else None


async def kitchen_for(session: AsyncSession, customer: Customer,
                      outlet_id: str | uuid.UUID | None = None) -> Outlet | None:
    """The kitchen picked for this customer's address.

    `outlet_id` (a conversation's stored choice) wins, then the customer's
    preferred kitchen. A kitchen switched off since then is not used; the
    default kitchen is the last resort.
    """
    for candidate in (outlet_id, customer.preferred_outlet_id):
        if not candidate:
            continue
        try:
            found = await session.get(Outlet, uuid.UUID(str(candidate)))
        except ValueError:
            log.warning("kitchen_id_invalid", value=str(candidate))
            continue
        if found is not None and found.is_active:
            return found
    return await get_kitchen(session)


async def resolve_location(
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    postal_code: str | None = None,
) -> GeoPoint | None:
    """Turn a shared pin or a typed ZIP into coordinates.

    A pin wins over a ZIP because it is exact. Returns None when neither can
    be resolved, which is the spec's "re-ask if location can't be read" path.
    """
    if latitude is not None and longitude is not None:
        return GeoPoint(latitude=float(latitude), longitude=float(longitude),
                        postal_code=(postal_code or "").strip(), source="pin")
    if postal_code:
        return await geocode_postal_code(postal_code)
    return None


async def check_service(session: AsyncSession, point: GeoPoint | None, *,
                        postal_code: str | None = None) -> ServiceCheck:
    """Do we deliver here, and from which kitchen? (spec step 10)."""
    kitchens = await active_kitchens(session)
    if not kitchens:
        log.error("no_kitchen_configured")
        return ServiceCheck(False, None,
                            "No kitchen has been set up yet in the admin dashboard.")

    zip_code = postal_code or (point.postal_code if point else "") or ""
    checks = coverage(kitchens, point, zip_code)
    serving = [c for c in checks if c.is_serviceable]
    # Nearest first; a kitchen with no distance (ZIP-only match) goes last.
    # sorted() is stable, so equal distances keep the primary kitchen first.
    pool = serving or checks
    best = sorted(pool, key=lambda c: (c.distance_km is None, c.distance_km or 0))[0]

    log.info("service_check", serviceable=best.is_serviceable,
             kitchen=best.kitchen.code if best.kitchen else None,
             distance=best.distance_km, kitchens=len(kitchens), covering=len(serving),
             zip=zip_code or None)
    if not best.is_serviceable:
        # Nothing covers it: report the nearest kitchen's reason, but do not
        # hand back a kitchen the order must not be placed with.
        return ServiceCheck(False, best.distance_km, best.reason,
                            kitchen_name=best.kitchen_name)
    return best


def coverage(kitchens: list[Outlet], point: GeoPoint | None,
             zip_code: str) -> list[ServiceCheck]:
    """How each kitchen stands against one customer location."""
    return [_evaluate(kitchen, point, zip_code) for kitchen in kitchens]


def _evaluate(kitchen: Outlet, point: GeoPoint | None, zip_code: str) -> ServiceCheck:
    """Does this one kitchen cover the address?"""
    distance = None
    if point is not None:
        distance = round_km(haversine_km(
            point.latitude, point.longitude, kitchen.latitude, kitchen.longitude
        ))

    mode = (kitchen.service_area_mode or "radius").lower()
    radius_ok = distance is not None and distance <= (kitchen.delivery_radius_km or 0)
    zip_ok = kitchen.serves_postal_code(zip_code)

    if mode == "zips":
        ok, reason = zip_ok, ("in the delivery ZIP list" if zip_ok
                              else "outside the delivery ZIP list")
    elif mode == "both":
        ok = radius_ok and zip_ok
        reason = _both_reason(radius_ok, zip_ok)
    elif distance is None:
        # Radius mode with no coordinates cannot decide; fail closed.
        ok, reason = False, "could not work out how far away that is"
    else:
        ok = radius_ok
        reason = (f"{distance} km from the kitchen" if ok
                  else f"{distance} km away, beyond the "
                       f"{kitchen.delivery_radius_km:g} km delivery range")
    return ServiceCheck(ok, distance, reason, kitchen_name=kitchen.name, kitchen=kitchen)


def _both_reason(radius_ok: bool, zip_ok: bool) -> str:
    if radius_ok and zip_ok:
        return "inside the delivery area"
    if not radius_ok and not zip_ok:
        return "outside the delivery range and not in the ZIP list"
    if not radius_ok:
        return "beyond the delivery range"
    return "not in the delivery ZIP list"


def miles(km: float | None) -> float | None:
    """A stored km radius as the miles the admin page shows."""
    return None if km is None else round(km / KM_PER_MILE, 2)


def km(miles_value: float) -> float:
    return round(miles_value * KM_PER_MILE, 3)
