"""Uber queue: every paid order, when it goes to Uber, and what happened.

The bot books the courier on its own (workers/jobs.dispatch_couriers). This
page shows the queue and lets an admin send one now - to test the Uber
connection, or to rescue an order - or cancel a booking.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import redirect, render, require_admin
from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import AdminUser, Order
from app.db.session import get_session
from app.services import dispatch
from app.services import kitchen as kitchen_service

log = get_logger(__name__)
router = APIRouter(prefix="/deliveries", tags=["admin"])


@router.get("", name="admin_deliveries")
async def queue_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    kitchens = await kitchen_service.active_kitchens(session)
    tz = _tz(kitchens[0].timezone if kitchens else None)
    rows = await dispatch.queue(session)
    return render(request, "admin/deliveries.html", {
        "current_user": current_user,
        "rows": rows,
        "tz": tz,
        "now": datetime.now(timezone.utc),
        "hours_before": settings.uber_dispatch_hours_before,
        "uber_ready": bool(settings.uber_customer_id and settings.uber_client_id
                           and settings.uber_client_secret),
        # Kitchens Uber cannot collect from: the courier needs a number.
        "no_phone": [k.name for k in kitchens if not (k.phone or k.kitchen_whatsapp)],
        "has_kitchen": bool(kitchens),
    })


@router.post("/{order_number}/send", name="admin_delivery_send")
async def send_now(
    request: Request,
    order_number: str,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    """Book the courier now instead of waiting for the send time."""
    url = str(request.url_for("admin_deliveries"))
    order = await _order(session, order_number)
    if order is None:
        return redirect(url, flash=("error", "That order no longer exists."))
    if order.uber_delivery_id and order.uber_delivery_status != "canceled":
        return redirect(url, flash=("warning", f"{order_number} already has a courier."))
    order.uber_delivery_id = None          # a cancelled booking may be sent again
    booked = await dispatch.dispatch(session, order)
    log.info("admin_delivery_sent", order_number=order_number, booked=booked,
             by=current_user.email)
    if booked:
        return redirect(url, flash=("success", f"{order_number} sent to Uber - courier booked."))
    return redirect(url, flash=("error", f"{order_number}: Uber did not accept it - "
                                         f"{order.uber_dispatch_error}"))


@router.post("/{order_number}/cancel", name="admin_delivery_cancel")
async def cancel(
    request: Request,
    order_number: str,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    url = str(request.url_for("admin_deliveries"))
    order = await _order(session, order_number)
    if order is None or not order.uber_delivery_id:
        return redirect(url, flash=("error", "That order has no Uber booking."))
    cancelled = await dispatch.cancel(order)
    log.info("admin_delivery_cancelled", order_number=order_number, ok=cancelled,
             by=current_user.email)
    if cancelled:
        return redirect(url, flash=("success", f"{order_number}: Uber booking cancelled."))
    return redirect(url, flash=("error", f"{order_number}: {order.uber_dispatch_error}"))


async def _order(session: AsyncSession, order_number: str) -> Order | None:
    return (await session.execute(select(Order).where(Order.order_number == order_number))
            ).scalar_one_or_none()


def _tz(name: str | None):
    try:
        return ZoneInfo(name or "America/New_York")
    except ZoneInfoNotFoundError:
        return timezone.utc
