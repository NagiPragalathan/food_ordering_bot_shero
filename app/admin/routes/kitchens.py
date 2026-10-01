"""Kitchens: add and edit any number of kitchens and their delivery areas.

A customer is served when their location is inside at least one active
kitchen's area; the nearest such kitchen cooks the order
(services/kitchen.check_service). The page also has an address checker that
shows how far an address is from every kitchen and which one would get it.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import redirect, render, require_admin
from app.core.logging import get_logger
from app.db.models import AdminUser, Outlet
from app.db.session import get_session
from app.integrations.geo.geocoder import geocode_address
from app.services import catalogue_sync, kitchen_admin, slots, zoho_connect
from app.services import kitchen as kitchen_service

log = get_logger(__name__)
router = APIRouter(prefix="/kitchens", tags=["admin"])


@router.get("", name="admin_kitchens")
async def kitchens_page(
    request: Request,
    q: str = "",
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    kitchens = list((await session.execute(
        select(Outlet).order_by(Outlet.is_primary.desc(), Outlet.created_at)
    )).scalars())
    return render(request, "admin/kitchens.html", {
        "current_user": current_user,
        "kitchens": kitchens,
        "miles": kitchen_service.miles,
        "check": await _address_check(session, q) if q.strip() else None,
        "q": q,
    })


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


# --- helpers -----------------------------------------------------------------
def _form(request: Request, current_user: AdminUser, kitchen: Outlet | None):
    return render(request, "admin/kitchen_form.html", {
        "current_user": current_user,
        "kitchen": kitchen,
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
