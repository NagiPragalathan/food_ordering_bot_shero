"""Razorpay webhook: DST India's payments.

Set up in the Razorpay Dashboard -> Accounts & Settings -> Webhooks:

    URL     https://<domain>/dst/webhooks/razorpay     (the business's own prefix)
    Secret  the value of DST_RAZORPAY_WEBHOOK_SECRET
    Events  payment_link.paid (payment_link.expired and .cancelled optional)

The URL is under the business that uses Razorpay (app/core/tenants.py), so its
own settings and database answer it. A business on Stripe refuses every call
here, and so does a bad signature: nothing is read before the signature
checks out.

The order is found by the payment link's id (stored when the link was made),
else by the order number in the link's notes. Paying is idempotent, so
Razorpay's retries - and the customer's own signed return to /pay/success,
which may confirm first - are safe. An expired or cancelled link goes through
the same check as the expiry job, which asks Razorpay itself.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import PaymentError
from app.core.logging import get_logger
from app.db.models import Customer, Order, PaymentStatus
from app.db.session import get_session
from app.integrations import payment_gateway
from app.integrations.razorpay_gw import checkout as razorpay_checkout
from app.services import orders as order_service
from app.services import payments

log = get_logger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

PAID_EVENTS = {"payment_link.paid"}
CLOSED_EVENTS = {"payment_link.expired", "payment_link.cancelled"}


@router.post("/razorpay", name="razorpay_webhook", status_code=status.HTTP_200_OK)
async def razorpay_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
    signature: str | None = Header(default=None, alias="X-Razorpay-Signature"),
) -> dict:
    if not payment_gateway.uses_razorpay():
        # This business pays through Stripe: Razorpay has nothing to say here.
        log.warning("razorpay_webhook_refused", reason="business does not use Razorpay")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Razorpay is not used here")

    payload = await request.body()
    try:
        event = razorpay_checkout.verify_webhook(payload, signature)
    except PaymentError as exc:
        log.warning("razorpay_webhook_rejected", error=str(exc))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    kind = str(event.get("event") or "")
    log.info("razorpay_webhook_received", kind=kind)
    if kind not in PAID_EVENTS | CLOSED_EVENTS:
        return {"status": "ignored", "type": kind}

    body = event.get("payload") or {}
    link = (body.get("payment_link") or {}).get("entity") or {}
    payment = (body.get("payment") or {}).get("entity") or {}

    order = await find_order(session, link)
    if order is None:
        # Not ours, or a test event fired from the Razorpay dashboard.
        log.warning("razorpay_webhook_no_order", link_id=link.get("id"))
        return {"status": "ignored", "reason": "order not found"}
    customer = await session.get(Customer, order.customer_id)
    if customer is None:
        log.error("razorpay_webhook_no_customer", order_number=order.order_number)
        return {"status": "ignored", "reason": "customer not found"}

    if kind in PAID_EVENTS:
        paid = await payments.handle_payment_success(
            session, order, customer, payment_intent_id=payment.get("id") or None)
        return {"status": "ok", "type": kind, "paid": paid}

    # Expired or cancelled at Razorpay. The link is closed for good, but ask
    # Razorpay before giving the slot away, exactly as the expiry job does -
    # a payment can land in the same moment. Already settled: nothing to do.
    if order.payment_status in (PaymentStatus.PAID, PaymentStatus.EXPIRED,
                                PaymentStatus.REFUNDED):
        return {"status": "ok", "type": kind, "outcome": "skipped"}
    outcome = await payments.settle_lapsed_link(session, order, customer)
    return {"status": "ok", "type": kind, "outcome": outcome}


async def find_order(session: AsyncSession, link: dict) -> Order | None:
    """The order a payment link belongs to: by the link id we stored, else by
    the order number in its notes."""
    link_id = str(link.get("id") or "")
    if link_id:
        found = await order_service.get_by_stripe_session(session, link_id)
        if found is not None:
            return found
    number = str((link.get("notes") or {}).get("order_number") or "")
    if number:
        return await order_service.get_by_number(session, number)
    return None
