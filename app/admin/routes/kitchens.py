"""Kitchens: add and edit any number of kitchens and their delivery areas.

A customer is served when their location is inside at least one active
kitchen's area; the nearest such kitchen cooks the order
(services/kitchen.check_service). The page also has an address checker that
shows how far an address is from every kitchen and which one would get it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from functools import partial

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import listing
from app.admin.deps import redirect, render, require_admin
from app.core.logging import get_logger
from app.db.models import AdminUser, Outlet
from app.db.session import get_session
from app.core.config import settings
from app.integrations.geo.geocoder import GeoPoint, geocode_address, reverse_geocode
from app.services import catalogue_sync, kitchen_admin, slots, zoho_connect
from app.services import kitchen as kitchen_service

log = get_logger(__name__)
router = APIRouter(prefix="/kitchens", tags=["admin"])


# The list's filters (the keys are the URL values).
STATUS_FILTERS = {"active": "Taking orders", "paused": "Paused", "open": "Open now"}


@router.get("", name="admin_kitchens")
async def kitchens_page(
    request: Request,
    q: str = "",
    find: str = "",
    status: str | None = None,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    """`find` searches the list; `q` is the address checker (as before)."""
    kitchens = list((await session.execute(
        select(Outlet).order_by(Outlet.is_primary.desc(), Outlet.created_at)
    )).scalars())
    now = datetime.now(timezone.utc)
    states = {k.id: kitchen_admin.open_state(k, now) for k in kitchens}
    chosen = listing.choice(status, STATUS_FILTERS, "")

    def shown(k: Outlet) -> bool:
        text = " ".join(str(v or "") for v in (k.name, k.address_line1, k.city, k.state,
                                                k.postal_code)).lower()
        if find.strip() and find.strip().lower() not in text:
            return False
        return {"active": k.is_active, "paused": not k.is_active,
                "open": k.is_active and states[k.id].open_now}.get(chosen, True)

    return render(request, "admin/kitchens.html", {
        "current_user": current_user,
        "kitchens": kitchens,
        "shown": [k for k in kitchens if shown(k)],
        "states": states,
        "stats": {
            "active": sum(1 for k in kitchens if k.is_active),
            "paused": sum(1 for k in kitchens if not k.is_active),
            "open": sum(1 for k in kitchens if k.is_active and states[k.id].open_now),
            "no_hours": sum(1 for k in kitchens if k.is_active and not k.operating_hours),
        },
        "status": chosen,
        "status_options": STATUS_FILTERS,
        "find": find,
        "url_with": partial(listing.url_with, request),
        "maps_browser_key": settings.google_maps_browser_key,
        "miles": kitchen_service.miles,
        "check": await _address_check(session, q) if q.strip() else None,
        "q": q,
    })


@router.get("/geo/lookup", name="admin_kitchen_geo")
async def geo_lookup(
    q: str = "",
    lat: float | None = None,
    lng: float | None = None,
    current_user: AdminUser = Depends(require_admin),
):
    """The kitchen form's map: an address for a dropped pin (lat, lng), or a
    pin for a searched address (q). Uses the server's geocoder, so it works
    with the browser key restricted to the Maps JavaScript API."""
    try:
        if lat is not None and lng is not None:
            point = await reverse_geocode(lat, lng)
        elif q.strip():
            point = await geocode_address(q.strip())
        else:
            return JSONResponse({"found": False, "error": "Nothing to look up."}, 400)
    except Exception as exc:  # noqa: BLE001 - the form still works by hand
        log.error("kitchen_geo_lookup_failed", error=str(exc))
        return JSONResponse({"found": False, "error": "The address lookup failed."}, 502)
    if point is None:
        return {"found": False, "error": "Could not find that on the map."}
    return {"found": True, **_address_fields(point, lat=lat, lng=lng)}


@router.get("/new", name="admin_kitchen_new")
async def new_kitchen(request: Request, current_user: AdminUser = Depends(require_admin)):
    return _form(request, current_user, None)


@router.get("/{kitchen_id}", name="admin_kitchen_edit")
async def edit_kitchen(
    request: Request,
    kitchen_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    kitchen = await session.get(Outlet, kitchen_id)
    if kitchen is None:
        return redirect(str(request.url_for("admin_kitchens")),
                        flash=("error", "That kitchen no longer exists."))
    return _form(request, current_user, kitchen)


@router.post("", name="admin_kitchen_create")
async def create_kitchen(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    return await _save(request, session, current_user, None)


@router.post("/{kitchen_id}", name="admin_kitchen_update")
async def update_kitchen(
    request: Request,
    kitchen_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    kitchen = await session.get(Outlet, kitchen_id)
    if kitchen is None:
        return redirect(str(request.url_for("admin_kitchens")),
                        flash=("error", "That kitchen no longer exists."))
    return await _save(request, session, current_user, kitchen)


@router.post("/{kitchen_id}/primary", name="admin_kitchen_primary")
async def make_primary(
    request: Request,
    kitchen_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    url = str(request.url_for("admin_kitchens"))
    kitchen = await session.get(Outlet, kitchen_id)
    if kitchen is None:
        return redirect(url, flash=("error", "That kitchen no longer exists."))
    await kitchen_admin.make_primary(session, kitchen)
    log.info("kitchen_made_primary", outlet=kitchen.code, by=current_user.email)
    return redirect(url, flash=("success", f"{kitchen.name} is now the default kitchen."))


@router.post("/{kitchen_id}/active", name="admin_kitchen_active")
async def set_active(
    request: Request,
    kitchen_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    """Pause or resume a kitchen from the list (the form's Taking orders switch)."""
    url = str(request.url_for("admin_kitchens"))
    kitchen = await session.get(Outlet, kitchen_id)
    if kitchen is None:
        return redirect(url, flash=("error", "That kitchen no longer exists."))
    active = (await request.form()).get("active") == "1"
    await kitchen_admin.set_active(session, kitchen, active)
    log.info("admin_kitchen_active", outlet=kitchen.code, active=active, by=current_user.email)
    if active:
        return redirect(url, flash=("success", f"{kitchen.name} is taking orders again."))
    others = await kitchen_service.active_kitchens(session)
    note = "" if others else " No kitchen is taking orders now, so customers cannot order."
    return redirect(url, flash=("warning" if note else "success",
                                f"{kitchen.name} is paused.{note}"))


# --- helpers -----------------------------------------------------------------
def _address_fields(point: GeoPoint, *, lat: float | None, lng: float | None) -> dict:
    """The form's address fields from a lookup. A dropped pin keeps its own
    position: the kitchen door beats Google's address centroid."""
    return {
        "lat": round(lat if lat is not None else point.latitude, 6),
        "lng": round(lng if lng is not None else point.longitude, 6),
        "street": point.street,
        "city": point.city,
        "state": point.state_code or point.state,
        "zip": point.postal_code,
        "label": point.formatted_address,
    }


def _form(request: Request, current_user: AdminUser, kitchen: Outlet | None):
    return render(request, "admin/kitchen_form.html", {
        "current_user": current_user,
        "kitchen": kitchen,
        # Public by design (referrer-restricted); see google_maps_browser_key.
        "maps_browser_key": settings.google_maps_browser_key,
        "radius_miles": (kitchen_service.miles(kitchen.delivery_radius_km) if kitchen
                         else kitchen_admin.DEFAULT_RADIUS_MILES),
    })


async def _save(request: Request, session: AsyncSession, current_user: AdminUser,
                kitchen: Outlet | None):
    form = await request.form()
    back = (str(request.url_for("admin_kitchen_edit", kitchen_id=kitchen.id)) if kitchen
            else str(request.url_for("admin_kitchen_new")))
    try:
        data = kitchen_admin.parse_form(form)
        saved = await kitchen_admin.save(session, kitchen, data)
    except kitchen_admin.KitchenFormError as exc:
        return redirect(back, flash=("error", str(exc)))

    notes = []
    if saved.is_active:
        try:
            # Old slots first (hours, length or timezone may have changed).
            await slots.rebuild_future(session, saved)
            await slots.ensure_slots(session, saved)
        except Exception as exc:  # noqa: BLE001 - the kitchen is saved; slots retry nightly
            log.error("kitchen_slots_failed", outlet=saved.code, error=str(exc))
            notes.append("delivery slots could not be generated yet")
    if zoho_connect.is_connected() and not await catalogue_sync.ensure_vendor(saved):
        notes.append("the Zoho Vendor could not be updated (see the logs)")

    log.info("kitchen_saved", outlet=saved.code, created=kitchen is None,
             active=saved.is_active, radius_km=saved.delivery_radius_km,
             open_days=len(saved.operating_hours), by=current_user.email)
    url = str(request.url_for("admin_kitchens"))
    if not saved.operating_hours:
        return redirect(url, flash=("warning", (
            f"Saved {saved.name}, but no opening hours are set, so it has no "
            "delivery slots. Edit it and set at least one day.")))
    if notes:
        return redirect(url, flash=("warning", f"Saved {saved.name}, but {'; '.join(notes)}."))
    return redirect(url, flash=("success", f"Saved {saved.name}."))


async def _address_check(session: AsyncSession, query: str) -> dict:
    """Which kitchen would get an order for this address, and why."""
    point = await geocode_address(query)
    if point is None:
        return {"error": "Could not find that address on the map."}
    kitchens = await kitchen_service.active_kitchens(session)
    result = await kitchen_service.check_service(session, point,
                                                 postal_code=point.postal_code)
    rows = sorted(kitchen_service.coverage(kitchens, point, point.postal_code or ""),
                  key=lambda c: (c.distance_km is None, c.distance_km or 0))
    return {
        "point": point,
        "result": result,
        "rows": [{"kitchen": c.kitchen, "serves": c.is_serviceable,
                  "miles": kitchen_service.miles(c.distance_km), "reason": c.reason}
                 for c in rows],
    }
