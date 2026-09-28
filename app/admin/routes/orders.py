"""Order queue: see live orders and move them through delivery."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import redirect, render, require_admin
from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import (
    AdminUser,
    Customer,
    Order,
    OrderStage,
    Outlet,
    PaymentStatus,
)
from app.db.session import get_session
from app.integrations.gallabox import templates as tpl
from app.integrations.gallabox.client import gallabox
from app.services import crm_sync
from app.services import orders as order_service

log = get_logger(__name__)
router = APIRouter(prefix="/orders", tags=["admin"])

PAGE_SIZE = 40

# Stage -> (next action label, target stage, template to send)
TRANSITIONS = {
    OrderStage.PAID_SLOT_BOOKED: ("Send to kitchen", OrderStage.SENT_TO_KITCHEN, None),
    OrderStage.SENT_TO_KITCHEN: ("Out for delivery", OrderStage.OUT_FOR_DELIVERY,
                                 tpl.ORDER_OUT_FOR_DELIVERY),
    OrderStage.OUT_FOR_DELIVERY: ("Mark delivered", OrderStage.DELIVERED,
                                  tpl.ORDER_DELIVERED),
}


def should_notify(template) -> bool:
    """Whether a stage change should also message the customer.

    The stage always advances; the message is optional. With
    `SEND_DELIVERY_UPDATES` off the kitchen still tracks an order through
    Out for delivery and Delivered - the customer simply is not told.
    """
    return template is not None and settings.send_delivery_updates


@router.get("", name="admin_orders")
async def orders_index(
    request: Request,
    stage: str | None = None,
    q: str | None = None,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    query = select(Order)
    if stage:
        query = query.where(Order.stage == stage)
    if q:
        query = query.where(Order.order_number.ilike(f"%{q.strip()}%"))

    rows = list((await session.execute(
        query.order_by(Order.created_at.desc()).limit(PAGE_SIZE)
    )).scalars())

    # Attach the customer and kitchen names for display.
    enriched = []
    for order in rows:
        customer = await session.get(Customer, order.customer_id)
        outlet = await session.get(Outlet, order.outlet_id) if order.outlet_id else None
        next_action = TRANSITIONS.get(OrderStage(order.stage)) if _is_known(order) else None
        enriched.append({
            "order": order,
            "customer": customer,
            "outlet": outlet,
            "next_label": next_action[0] if next_action else None,
            "next_stage": str(next_action[1]) if next_action else None,
        })

    counts = {}
    for value in OrderStage:
        counts[str(value)] = int(await session.scalar(
            select(func.count(Order.id)).where(Order.stage == str(value))
        ) or 0)

    return render(request, "admin/orders.html", {
        "current_user": current_user,
        "rows": enriched,
        "counts": counts,
        "stages": [str(s) for s in OrderStage],
        "selected_stage": stage or "",
        "query": q or "",
    })


@router.post("/{order_number}/advance", name="admin_order_advance")
async def advance_order(
    request: Request,
    order_number: str,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    """Move an order to its next stage and notify the customer."""
    url = str(request.url_for("admin_orders"))

    order = await order_service.get_by_number(session, order_number.upper())
    if order is None:
        return redirect(url, flash=("error", f"Order {order_number} not found."))

    if order.payment_status != PaymentStatus.PAID:
        return redirect(url, flash=(
            "error", f"{order.order_number} is not paid, so it cannot be advanced."
        ))

    transition = TRANSITIONS.get(OrderStage(order.stage)) if _is_known(order) else None
    if transition is None:
        return redirect(url, flash=(
            "error", f"{order.order_number} is {order.stage} - no further step."
        ))

    _, target, template = transition
    if not order_service.set_stage(order, target):
        return redirect(url, flash=("success", "Already at that stage."))
    await session.flush()

    customer = await session.get(Customer, order.customer_id)
    if should_notify(template) and customer is not None:
        try:
            await gallabox.send_template(customer.whatsapp_number, template,
                                         order.order_number)
        except Exception as exc:  # noqa: BLE001 - stage change already committed
            log.error("order_notify_failed", order_number=order.order_number,
                      error=str(exc))
            await crm_sync.push_order_stage(order, target)
            return redirect(url, flash=(
                "warning",
                f"{order.order_number} moved to {target}, but the WhatsApp "
                f"message failed to send: {exc}",
            ))

    await crm_sync.push_order_stage(
        order, target,
        delivered_at=order.delivered_at if target is OrderStage.DELIVERED else None,
    )
    log.info("admin_order_advanced", order_number=order.order_number,
             stage=str(target), by=current_user.email)
    return redirect(url, flash=(
        "success", f"{order.order_number} is now {target}."
    ))


def _is_known(order: Order) -> bool:
    """Guard against a stage value that is not in the enum."""
    return order.stage in {str(s) for s in OrderStage}
