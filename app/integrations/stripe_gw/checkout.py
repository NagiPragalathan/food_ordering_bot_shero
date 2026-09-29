"""Stripe Checkout (spec steps 15-16).

The session is created with an explicit `expires_at` so Stripe itself enforces
the 30-minute link lifetime and emits `checkout.session.expired`; our scheduler
then releases the held slot. (If this machine's clock is wrong Stripe rejects
that expiry; see `_create` for what happens then.) Line items are itemised (dishes, delivery, taxes
and fees) so the Stripe page shows the same breakdown as the WhatsApp summary.

Package name is `stripe_gw` rather than `stripe` so it cannot shadow the SDK.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import stripe

from app.core.config import is_unset, settings
from app.core.exceptions import PaymentError
from app.core.logging import get_logger

log = get_logger(__name__)

# Stripe rejects an expiry closer than 30 minutes or further than 24 hours.
MIN_EXPIRY_MINUTES = 30
MAX_EXPIRY_MINUTES = 24 * 60


# With no STRIPE_SECRET_KEY the checkout is mocked: the order flow and the
# WhatsApp summary run end to end, and Pay Now opens a "test payment" page
# instead of Stripe. Nothing is charged and no webhook will mark it paid.
MOCK_SESSION_PREFIX = "mock_"


def mock_mode() -> bool:
    """True while no Stripe key is configured."""
    return is_unset(settings.stripe_secret_key)


def mock_checkout_url(order_number: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/pay/mock/{order_number}"


def _apply_api_key() -> None:
    """Apply the API key to the module-level SDK before each use."""
    stripe.api_key = settings.stripe_secret_key


def to_minor_units(amount: Decimal) -> int:
    """Decimal currency amount -> integer cents."""
    return int((Decimal(amount) * 100).to_integral_value())


def clamp_expiry_minutes(minutes: int) -> int:
    """Keep the configured TTL inside the window Stripe accepts."""
    return max(MIN_EXPIRY_MINUTES, min(minutes, MAX_EXPIRY_MINUTES))


def build_line_items(
    *,
    items: list[dict],
    delivery_fee: Decimal,
    taxes_and_fees: Decimal,
    currency: str,
) -> list[dict]:
    """Itemised Checkout lines matching the WhatsApp order summary."""
    lines: list[dict] = []
    for item in items:
        quantity = int(item.get("quantity", 1))
        unit_price = Decimal(str(item.get("unit_price", "0")))
        if quantity <= 0 or unit_price < 0:
            continue
        lines.append({
            "price_data": {
                "currency": currency,
                "unit_amount": to_minor_units(unit_price),
                "product_data": {"name": str(item.get("name") or "Item")[:250]},
            },
            "quantity": quantity,
        })

    if delivery_fee > 0:
        lines.append({
            "price_data": {
                "currency": currency,
                "unit_amount": to_minor_units(delivery_fee),
                "product_data": {"name": "Delivery charge"},
            },
            "quantity": 1,
        })

    if taxes_and_fees > 0:
        lines.append({
            "price_data": {
                "currency": currency,
                "unit_amount": to_minor_units(taxes_and_fees),
                "product_data": {"name": "Taxes and fees"},
            },
            "quantity": 1,
        })

    if not lines:
        raise PaymentError("cannot create a Stripe session with no line items")
    return lines


async def create_checkout_session(
    *,
    order_id: str,
    order_number: str,
    items: list[dict],
    delivery_fee: Decimal,
    taxes_and_fees: Decimal,
    currency: str | None = None,
    customer_email: str | None = None,
    customer_phone: str | None = None,
    ttl_minutes: int | None = None,
) -> dict:
    """Create the Checkout Session and return {id, url, expires_at}."""
    currency = (currency or settings.stripe_currency).lower()
    minutes = clamp_expiry_minutes(ttl_minutes or settings.payment_link_ttl_minutes)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)

    if mock_mode():
        log.warning("stripe_mock_checkout", order_number=order_number,
                    reason="STRIPE_SECRET_KEY is not set")
        return {"id": f"{MOCK_SESSION_PREFIX}{order_number}",
                "url": mock_checkout_url(order_number), "expires_at": expires_at}

    _apply_api_key()

    params: dict = {
        "mode": "payment",
        "line_items": build_line_items(
            items=items,
            delivery_fee=delivery_fee,
            taxes_and_fees=taxes_and_fees,
            currency=currency,
        ),
        # Stripe wants at least 30 minutes; a minute's margin covers the
        # request's own latency.
        "expires_at": int(expires_at.timestamp()) + 60,
        "client_reference_id": order_number,
        # Echoed back on every webhook, so the order can be found without a
        # separate lookup table.
        "metadata": {
            "order_id": order_id,
            "order_number": order_number,
            "whatsapp_number": customer_phone or "",
        },
        "payment_intent_data": {
            "metadata": {"order_id": order_id, "order_number": order_number}
        },
        "success_url": f"{settings.public_base_url}/pay/success/{order_number}",
        "cancel_url": f"{settings.public_base_url}/pay/cancelled/{order_number}",
    }
    if customer_email:
        params["customer_email"] = customer_email

    try:
        session = await _create(params, order_number)
    except stripe.StripeError as exc:
        log.error("stripe_session_failed", order_number=order_number, error=str(exc))
        raise PaymentError(f"Stripe checkout creation failed: {exc}") from exc

    log.info("stripe_session_created", order_number=order_number,
             session_id=session.id, expires_at=expires_at.isoformat())
    return {"id": session.id, "url": session.url, "expires_at": expires_at}


async def _create(params: dict, order_number: str):
    """Create the session, surviving a wrong clock on this machine.

    Stripe judges `expires_at` by its own clock. If it says our "30 minutes
    from now" is already in the past, this machine's clock is wrong; the
    link still has to reach the customer, so the session is created without
    an expiry (Stripe's default is 24 hours) and the bot's own expiry job,
    which runs on this machine's clock, still expires it at 30 minutes. The
    error is loud because a wrong clock also makes Stripe reject every
    webhook signature - the clock has to be fixed, not the code.
    """
    try:
        return await stripe.checkout.Session.create_async(**params)
    except stripe.InvalidRequestError as exc:
        if getattr(exc, "param", None) != "expires_at" or "expires_at" not in params:
            raise
        log.error("stripe_expiry_rejected_machine_clock_wrong",
                  order_number=order_number, error=str(exc))
        return await stripe.checkout.Session.create_async(
            **{k: v for k, v in params.items() if k != "expires_at"})


async def expire_session(session_id: str) -> None:
    """Expire a session early (slot released, or order cancelled)."""
    if session_id.startswith(MOCK_SESSION_PREFIX):
        return   # nothing exists at Stripe to expire
    _apply_api_key()
    try:
        await stripe.checkout.Session.expire_async(session_id)
    except stripe.StripeError as exc:
        # Already expired or already paid - not worth failing the caller over.
        log.info("stripe_session_expire_skipped", session_id=session_id, error=str(exc))


async def create_refund(payment_intent_id: str,
                        amount: Decimal | None = None) -> dict:
    """Full or partial refund (spec: Cancelled / Refunded order stage)."""
    _apply_api_key()
    params: dict = {"payment_intent": payment_intent_id}
    if amount is not None:
        params["amount"] = to_minor_units(amount)
    try:
        refund = await stripe.Refund.create_async(**params)
    except stripe.StripeError as exc:
        log.error("stripe_refund_failed", payment_intent=payment_intent_id, error=str(exc))
        raise PaymentError(f"Stripe refund failed: {exc}") from exc

    log.info("stripe_refund_created", refund_id=refund.id,
             payment_intent=payment_intent_id)
    return {
        "id": refund.id,
        "amount": Decimal(refund.amount) / 100,
        "status": refund.status,
    }


def verify_webhook(payload: bytes, signature_header: str | None) -> stripe.Event:
    """Validate the Stripe-Signature header and return the parsed event.

    Raises PaymentError on a bad signature so the route can answer 400 without
    processing anything.
    """
    if not settings.stripe_webhook_secret:
        raise PaymentError("STRIPE_WEBHOOK_SECRET is not configured")
    if not signature_header:
        raise PaymentError("missing Stripe-Signature header")
    try:
        return stripe.Webhook.construct_event(
            payload, signature_header, settings.stripe_webhook_secret
        )
    except ValueError as exc:
        raise PaymentError(f"malformed Stripe payload: {exc}") from exc
    except stripe.SignatureVerificationError as exc:
        raise PaymentError(f"invalid Stripe signature: {exc}") from exc
