"""Outlet operations API (spec steps 17-18).

This is the "backend" the outlet marks orders in: Out for Delivery, Delivered,
Cancelled. Each transition notifies the customer on the matching approved
template and mirrors the stage into Zoho.

Protected by a shared `X-Ops-Key` header. That is adequate for a handful of
trusted kitchen tablets; if this ever becomes a multi-user dashboard, move it
behind real per-user auth.
"""

from __future__ import annotations

import hmac
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import PaymentError
from app.core.logging import get_logger
from app.db.models import Customer, Order, OrderStage, Outlet, PaymentStatus
from app.db.session import get_session
from app.integrations.gallabox import templates as tpl
from app.integrations.gallabox.client import gallabox
from app.services import crm_sync
from app.services import orders as order_service
from app.services import payments

log = get_logger(__name__)
router = APIRouter(prefix="/ops", tags=["ops"])


def require_ops_key(x_ops_key: str | None = Header(default=None)) -> None:
    """Shared-key auth, compared in constant time."""
    if not settings.ops_api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "OPS_API_KEY is not configured")
    if not x_ops_key or not hmac.compare_digest(x_ops_key, settings.ops_api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid ops key")


class OrderSummary(BaseModel):
    order_number: str
    stage: str
    payment_status: str
    outlet: str | None = None
    customer_name: str | None = None
    contact_number: str | None = None
    delivery_address: str | None = None
    apartment_unit: str | None = None
    delivery_instructions: str | None = None
    slot_label: str | None = None
    items: list[dict] = Field(default_factory=list)
    total: str
    currency: str


class RefundRequest(BaseModel):
    amount: Decimal | None = Field(
        default=None, gt=0,
        description="Partial refund amount; omit for a full refund.",
    )


@router.get("/orders", response_model=list[OrderSummary],
            dependencies=[Depends(require_ops_key)])
async def list_kitchen_orders(
    outlet_code: str | None = None,
    stage: str | None = None,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
) -> list[OrderSummary]:
    """Paid orders for the kitchen queue, newest first."""
    query = select(Order).where(Order.payment_status == PaymentStatus.PAID)

    if stage:
        query = query.where(Order.stage == stage)
    if outlet_code:
        outlet = await session.execute(
            select(Outlet).where(Outlet.code == outlet_code)
        )
        found = outlet.scalar_one_or_none()
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown outlet code")
        query = query.where(Order.outlet_id == found.id)

    query = query.order_by(Order.created_at.desc()).limit(min(limit, 200))
    result = await session.execute(query)

    summaries = []
    for order in result.scalars():
        customer = await session.get(Customer, order.customer_id)
        outlet = await session.get(Outlet, order.outlet_id) if order.outlet_id else None
        summaries.append(_summarise(order, customer, outlet))
    return summaries


@router.post("/orders/{order_number}/out-for-delivery",
             dependencies=[Depends(require_ops_key)])
async def mark_out_for_delivery(
    order_number: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Step 18: outlet marks the order out for delivery."""
    return await _transition(
        session, order_number, OrderStage.OUT_FOR_DELIVERY,
        template=tpl.ORDER_OUT_FOR_DELIVERY,
    )


@router.post("/orders/{order_number}/delivered",
             dependencies=[Depends(require_ops_key)])
async def mark_delivered(
    order_number: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Step 18: outlet marks the order delivered.

    The feedback request is not sent here - the scheduler sends it 30 minutes
    later (spec step 19).
    """
    return await _transition(
        session, order_number, OrderStage.DELIVERED,
        template=tpl.ORDER_DELIVERED,
    )


@router.post("/orders/{order_number}/cancel",
             dependencies=[Depends(require_ops_key)])
async def cancel_order(
    order_number: str,
    body: RefundRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Cancel a paid order and start the refund."""
    order, customer = await _load(session, order_number)

    order_service.set_stage(order, OrderStage.CANCELLED)
    await session.flush()

    await gallabox.send_template(
        customer.whatsapp_number, tpl.ORDER_CANCELLED, order.order_number
    )

    refunded = False
    if order.payment_status == PaymentStatus.PAID:
        try:
            refunded = await payments.refund_order(
                session, order, customer, body.amount if body else None
            )
        except PaymentError as exc:
            log.error("cancel_refund_failed", order_number=order_number,
                      error=str(exc))
            raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                                f"order cancelled but refund failed: {exc}") from exc

    await crm_sync.push_order_stage(order, OrderStage(order.stage))
    return {"order_number": order.order_number, "stage": order.stage,
            "refunded": refunded}


# --- internals ---------------------------------------------------------------
async def _transition(session: AsyncSession, order_number: str,
                      stage: OrderStage, *, template) -> dict:
    order, customer = await _load(session, order_number)

    changed = order_service.set_stage(order, stage)
    if not changed:
        # Idempotent: a tablet double-tap must not send two notifications.
        return {"order_number": order.order_number, "stage": order.stage,
                "changed": False}

    await session.flush()
    await gallabox.send_template(customer.whatsapp_number, template,
                                 order.order_number)
    await crm_sync.push_order_stage(
        order, stage,
        delivered_at=order.delivered_at if stage is OrderStage.DELIVERED else None,
    )
    log.info("ops_stage_set", order_number=order.order_number, stage=str(stage))
    return {"order_number": order.order_number, "stage": order.stage,
            "changed": True}


async def _load(session: AsyncSession,
                order_number: str) -> tuple[Order, Customer]:
    order = await order_service.get_by_number(session, order_number.upper())
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown order")

    customer = await session.get(Customer, order.customer_id)
    if customer is None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "order has no linked customer")
    return order, customer


def _summarise(order: Order, customer: Customer | None,
               outlet: Outlet | None) -> OrderSummary:
    return OrderSummary(
        order_number=order.order_number,
        stage=order.stage,
        payment_status=order.payment_status,
        outlet=outlet.name if outlet else None,
        customer_name=customer.name if customer else None,
        contact_number=order.contact_number,
        delivery_address=order.delivery_address,
        apartment_unit=order.apartment_unit,
        delivery_instructions=order.delivery_instructions,
        slot_label=order.slot_label,
        items=order.items or [],
        total=f"{order.total:.2f}",
        currency=order.currency,
    )
