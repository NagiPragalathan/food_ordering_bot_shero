"""The single kitchen and its delivery area (spec steps 6a, 9, 10).

Replaces the multi-outlet "pick your nearest branch" flow. There is one
kitchen, configured on the admin Settings page, and the only question is
whether we deliver to the customer's address.

Serviceability is decided by the kitchen's `service_area_mode`:

    radius  distance <= delivery_radius_km
    zips    the postal code is in the kitchen's list
    both    inside the radius AND in the list

A ZIP list is the honest option for a small operation: it says exactly where
the driver will go, rather than drawing a circle over a river.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Outlet
from app.integrations.geo.distance import haversine_km, round_km
from app.integrations.geo.geocoder import GeoPoint, geocode_postal_code

log = get_logger(__name__)


@dataclass(frozen=True)
class ServiceCheck:
    is_serviceable: bool
    distance_km: float | None
    reason: str
    kitchen_name: str = ""

    @property
    def distance_display(self) -> str:
        return f"{self.distance_km} km" if self.distance_km is not None else ""


async def get_kitchen(session: AsyncSession) -> Outlet | None:
    """The one active kitchen.

    Prefers the row flagged primary, then the oldest active one, so a second
    kitchen added later does not silently take over.
    """
    result = await session.execute(
        select(Outlet)
        .where(Outlet.is_active.is_(True))
        .order_by(Outlet.is_primary.desc(), Outlet.created_at)
    )
    return result.scalars().first()


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
    """Do we deliver here? (spec step 10)."""
    kitchen = await get_kitchen(session)
    if kitchen is None:
        log.error("no_kitchen_configured")
        return ServiceCheck(False, None,
                            "No kitchen has been set up yet in the admin dashboard.")

    zip_code = postal_code or (point.postal_code if point else "") or ""
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
    else:
        if distance is None:
            # Radius mode with no coordinates cannot decide; fail closed.
            ok, reason = False, "could not work out how far away that is"
        else:
            ok = radius_ok
            reason = (f"{distance} km from the kitchen" if ok
                      else f"{distance} km away, beyond the "
                           f"{kitchen.delivery_radius_km:g} km delivery range")

    log.info("service_check", mode=mode, serviceable=ok, distance=distance,
             zip=zip_code or None)
    return ServiceCheck(ok, distance, reason, kitchen_name=kitchen.name)


def _both_reason(radius_ok: bool, zip_ok: bool) -> str:
    if radius_ok and zip_ok:
        return "inside the delivery area"
    if not radius_ok and not zip_ok:
        return "outside the delivery range and not in the ZIP list"
    if not radius_ok:
        return "beyond the delivery range"
    return "not in the delivery ZIP list"
