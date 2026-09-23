"""Stripe webhook (spec step 16).

Signature is verified before anything is read. Each event type maps to one
idempotent service call, so Stripe's at-least-once delivery is safe.

The order is located from `metadata.order_id`, which we set when creating the
session, falling back to the session or payment-intent id.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import PaymentError
from app.core.logging import get_logger
from app.db.models import Customer, Order
from app.db.session import get_session
from app.integrations.stripe_gw.checkout import verify_webhook
from app.services import orders as order_service
from app.services import payments

log = get_logger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

HANDLED_EVENTS = {
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "checkout.session.expired",
    "checkout.session.async_payment_failed",
    "payment_intent.payment_failed",
    "charge.refunded",
}


@router.post("/stripe", status_code=status.HTTP_200_OK)
async def stripe_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
) -> dict:
    payload = await request.body()
    try:
        event = verify_webhook(payload, stripe_signature)
    except PaymentError as exc:
        log.warning("stripe_webhook_rejected", error=str(exc))
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    event_type = event["type"]
    obj = event["data"]["object"]
    log.info("stripe_webhook_received", type=event_type, id=event.get("id"))

    if event_type not in HANDLED_EVENTS:
        return {"status": "ignored", "type": event_type}

    order = await _find_order(session, obj)
    if order is None:
        # Not ours, or a test event fired from the Stripe dashboard.
        log.warning("stripe_webhook_no_order", type=event_type)
        return {"status": "ignored", "reason": "order not found"}

    customer = await session.get(Customer, order.customer_id)
    if customer is None:
        log.error("stripe_webhook_no_customer", order_number=order.order_number)
        return {"status": "ignored", "reason": "customer not found"}

    match event_type:
        case "checkout.session.completed" | "checkout.session.async_payment_succeeded":
            # A session can complete without being paid when it uses a delayed
            # payment method, so the payment status is what we trust.
            if obj.get("payment_status") in ("paid", "no_payment_required"):
                await payments.handle_payment_success(
                    session, order, customer,
                    payment_intent_id=_as_id(obj.get("payment_intent")),
                )
            else:
                log.info("stripe_session_completed_unpaid",
                         order_number=order.order_number,
                         payment_status=obj.get("payment_status"))

        case "checkout.session.expired":
            await payments.handle_payment_expired(session, order, customer)

        case "checkout.session.async_payment_failed" | "payment_intent.payment_failed":
            await payments.handle_payment_failed(session, order, customer)

        case "charge.refunded":
            # Refunds we initiate are already recorded; this covers a refund
            # issued from the Stripe dashboard by hand.
            await _record_dashboard_refund(session, order, obj)

    return {"status": "ok", "type": event_type}


async def _find_order(session: AsyncSession, obj: dict) -> Order | None:
    metadata = obj.get("metadata") or {}

    order_id = metadata.get("order_id")
    if order_id:
        order = await order_service.get_order(session, order_id)
        if order:
            return order

    order_number = metadata.get("order_number") or obj.get("client_reference_id")
    if order_number:
        order = await order_service.get_by_number(session, order_number)
        if order:
            return order

    object_type = obj.get("object")
    if object_type == "checkout.session" and obj.get("id"):
        return await order_service.get_by_stripe_session(session, obj["id"])

    intent_id = _as_id(obj.get("payment_intent")) or (
        obj.get("id") if object_type == "payment_intent" else None
    )
    if intent_id:
        return await order_service.get_by_payment_intent(session, intent_id)

    return None


async def _record_dashboard_refund(session: AsyncSession, order: Order,
                                   charge: dict) -> None:
    """Mirror a refund that was issued outside this service."""
    from decimal import Decimal

    from app.db.models import OrderStage, PaymentStatus

    if order.payment_status == PaymentStatus.REFUNDED:
        return

    refunded_minor = charge.get("amount_refunded") or 0
    order.payment_status = PaymentStatus.REFUNDED
    order.refund_amount = Decimal(refunded_minor) / 100
    order_service.set_stage(order, OrderStage.REFUNDED)
    await session.flush()
    log.info("refund_recorded_from_stripe", order_number=order.order_number,
             amount=str(order.refund_amount))


def _as_id(value) -> str | None:
    """Stripe sends a reference as either a bare id or an expanded object."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("id")
    return None
