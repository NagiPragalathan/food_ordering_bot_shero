"""Razorpay Payment Links: how a business on PAYMENT_PROVIDER=razorpay
(DST India) takes the money.

The same functions as the Stripe checkout (integrations/stripe_gw/checkout.py),
returning the same shapes, so services/payments.py and the Pay Now redirect
treat both alike through integrations/payment_gateway.py:

    create_checkout_session -> {id, url, expires_at}   the link Pay Now opens
    session_state           -> {status, payment_status, payment_intent}
    expire_session                                      no paying after the slot is gone
    create_refund           -> {id, amount, status}
    verify_webhook / verify_callback                    Razorpay's signatures

The order's stripe_session_id, stripe_payment_intent_id and stripe_refund_id
columns hold the link id (plink_...), payment id (pay_...) and refund id
(rfnd_...).

Razorpay takes amounts in paise and keeps a link open at least 15 minutes.
UPI, cards and netbanking are all on Razorpay's own page, which shows the
order's breakdown in the link's description (a link has one amount, not lines).

With no RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET the link is mocked like Stripe's:
Pay Now opens the "test payment" page (/pay/mock/<order>), nothing is charged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from app.core import locale
from app.core.config import is_unset, settings
from app.core.exceptions import IntegrationError, PaymentError
from app.core.logging import get_logger
from app.integrations.base import ApiClient

log = get_logger(__name__)

# Razorpay refuses an expire_by closer than 15 minutes; a minute's margin
# covers the request's own latency.
MIN_EXPIRY_MINUTES = 16
MAX_EXPIRY_MINUTES = 24 * 60
MOCK_SESSION_PREFIX = "mock_"
DESCRIPTION_LIMIT = 2000
REFERENCE_LIMIT = 40          # Razorpay's limit on reference_id

# Razorpay's link statuses -> the Stripe-shaped state payments.py reads.
OPEN = ("created", "partially_paid")
PAID = "paid"


def mock_mode() -> bool:
    """True while this business has no Razorpay keys."""
    return is_unset(settings.razorpay_key_id) or is_unset(settings.razorpay_key_secret)


def test_mode() -> bool:
    """A test-mode key (rzp_test_...): no real money moves."""
    return settings.razorpay_key_id.strip().startswith("rzp_test_")


def mock_checkout_url(order_number: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/pay/mock/{order_number}"


def to_paise(amount) -> int:
    return int((Decimal(str(amount)) * 100).to_integral_value())


def clamp_expiry_minutes(minutes: int) -> int:
    return max(MIN_EXPIRY_MINUTES, min(int(minutes), MAX_EXPIRY_MINUTES))


class RazorpayClient(ApiClient):
    """Basic auth with the current business's key id and secret."""

    service = "razorpay"

    def __init__(self) -> None:
        super().__init__(base_url=settings.razorpay_base_url.rstrip("/"))

    async def default_headers(self) -> dict[str, str]:
        pair = f"{settings.razorpay_key_id}:{settings.razorpay_key_secret}".encode()
        return {"Authorization": "Basic " + base64.b64encode(pair).decode(),
                "Content-Type": "application/json"}


razorpay_client = RazorpayClient()


async def _api(method: str, path: str, *, json_body: dict | None = None,
               params: dict | None = None) -> Any:
    """One call to Razorpay. The single place tests replace."""
    return await razorpay_client.request(method, path, json=json_body, params=params)


# --- the link ----------------------------------------------------------------
def order_total(*, items: list[dict], delivery_fee: Decimal, taxes_and_fees: Decimal,
                discount: Decimal) -> Decimal:
    """What the link charges: the same sum Stripe charges, line for line."""
    dishes = sum((Decimal(str(i.get("unit_price", "0"))) * int(i.get("quantity", 1))
                  for i in items if int(i.get("quantity", 1)) > 0), Decimal("0"))
    return dishes + Decimal(delivery_fee) + Decimal(taxes_and_fees) - Decimal(discount or 0)


def describe(*, order_number: str, items: list[dict], delivery_fee: Decimal,
             taxes_and_fees: Decimal, discount: Decimal, discount_label: str | None) -> str:
    """The bill on Razorpay's page: "2 x Drumstick Sambar ₹498.00 · Delivery
    ₹40.00 · Taxes and fees ₹12.45 · Coupon SHERO10 -₹50.00"."""
    parts = []
    for item in items:
        quantity = int(item.get("quantity", 1))
        if quantity <= 0:
            continue
        line = Decimal(str(item.get("unit_price", "0"))) * quantity
        name = str(item.get("name") or "Item")
        parts.append(f"{quantity} x {name} {locale.money(line)}")
    if Decimal(delivery_fee) > 0:
        parts.append(f"Delivery {locale.money(delivery_fee)}")
    if Decimal(taxes_and_fees) > 0:
        parts.append(f"Taxes and fees {locale.money(taxes_and_fees)}")
    if discount and Decimal(discount) > 0:
        parts.append(f"{discount_label or 'Discount'} -{locale.money(discount)}")
    text = f"{settings.business_name} order {order_number}: " + " · ".join(parts)
    return text[:DESCRIPTION_LIMIT]


def build_link(*, order_id: str, order_number: str, items: list[dict],
               delivery_fee: Decimal, taxes_and_fees: Decimal, currency: str | None,
               customer_email: str | None, customer_phone: str | None,
               expires_at: datetime, discount: Decimal = Decimal("0"),
               discount_label: str | None = None, now: datetime | None = None) -> dict:
    """The body of POST /payment_links."""
    total = order_total(items=items, delivery_fee=delivery_fee,
                        taxes_and_fees=taxes_and_fees, discount=discount)
    if total <= 0:
        raise PaymentError(f"the total of {order_number} is not above zero")
    now = now or datetime.now(timezone.utc)
    contact = "".join(ch for ch in customer_phone or "" if ch.isdigit())
    customer: dict[str, str] = {}
    if contact:
        customer["contact"] = f"+{contact}"
    if customer_email:
        customer["email"] = customer_email
    return {
        "amount": to_paise(total),
        "currency": (currency or settings.currency or "INR").upper(),
        "accept_partial": False,
        # A minute past our own expiry: the expiry job asks Razorpay first.
        "expire_by": int(expires_at.timestamp()) + 60,
        # Unique per link: a second link for the same order gets its own.
        "reference_id": f"{order_number}-{int(now.timestamp())}"[:REFERENCE_LIMIT],
        "description": describe(order_number=order_number, items=items,
                                delivery_fee=delivery_fee, taxes_and_fees=taxes_and_fees,
                                discount=discount, discount_label=discount_label),
        "customer": customer,
        # WhatsApp carries the link; Razorpay sends no SMS or email of its own.
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "notes": {"order_id": order_id, "order_number": order_number},
        "callback_url": f"{settings.public_base_url.rstrip('/')}/pay/success/{order_number}",
        "callback_method": "get",
    }


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
    discount: Decimal = Decimal("0"),
    discount_label: str | None = None,
) -> dict:
    """Create the payment link and return {id, url, expires_at}."""
    minutes = clamp_expiry_minutes(ttl_minutes or settings.payment_link_ttl_minutes)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    body = build_link(order_id=order_id, order_number=order_number, items=items,
                      delivery_fee=delivery_fee, taxes_and_fees=taxes_and_fees,
                      currency=currency, customer_email=customer_email,
                      customer_phone=customer_phone, expires_at=expires_at,
                      discount=discount, discount_label=discount_label)

    if mock_mode():
        log.warning("razorpay_mock_link", order_number=order_number,
                    reason="RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set - nothing is charged")
        return {"id": f"{MOCK_SESSION_PREFIX}{order_number}",
                "url": mock_checkout_url(order_number), "expires_at": expires_at}

    try:
        created = await _api("POST", "/payment_links", json_body=body)
    except IntegrationError as exc:
        log.error("razorpay_link_failed", order_number=order_number, error=str(exc))
        raise PaymentError(f"Razorpay payment link failed: {exc}") from exc
    link_id = (created or {}).get("id")
    url = (created or {}).get("short_url")
    if not link_id or not url:
        raise PaymentError(f"Razorpay returned no link for {order_number}")
    log.info("razorpay_link_created", order_number=order_number, link_id=link_id,
             expires_at=expires_at.isoformat())
    return {"id": link_id, "url": url, "expires_at": expires_at}


async def session_state(link_id: str) -> dict | None:
    """What Razorpay says about a link, shaped like Stripe's session state:
    {status: open|complete|expired, payment_status: paid|unpaid,
    payment_intent: the pay_... id}. None for a mock link.

    Raises PaymentError when Razorpay cannot be asked, so a caller never takes
    "could not check" for "not paid".
    """
    if not link_id or link_id.startswith(MOCK_SESSION_PREFIX):
        return None
    try:
        found = await _api("GET", f"/payment_links/{link_id}")
    except IntegrationError as exc:
        log.error("razorpay_link_lookup_failed", link_id=link_id, error=str(exc))
        raise PaymentError(f"Razorpay link lookup failed: {exc}") from exc
    return state_of(found or {})


def state_of(link: dict) -> dict:
    """A Razorpay payment link entity as the Stripe-shaped state."""
    status = str(link.get("status") or "").lower()
    payments = link.get("payments") or []
    taken = next((p for p in payments
                  if str(p.get("status") or "").lower() in ("captured", "authorized")), None)
    payment_id = (taken or {}).get("payment_id") or (taken or {}).get("id")
    if status == PAID:
        return {"status": "complete", "payment_status": "paid", "payment_intent": payment_id}
    if status in OPEN:
        return {"status": "open", "payment_status": "unpaid", "payment_intent": payment_id}
    return {"status": "expired", "payment_status": "unpaid", "payment_intent": payment_id}


async def expire_session(link_id: str) -> None:
    """Cancel a link early (slot released, order changed or cancelled)."""
    if not link_id or link_id.startswith(MOCK_SESSION_PREFIX):
        return
    try:
        await _api("POST", f"/payment_links/{link_id}/cancel")
    except IntegrationError as exc:
        # Already paid, expired or cancelled - not worth failing the caller over.
        log.info("razorpay_link_cancel_skipped", link_id=link_id, error=str(exc))


async def create_refund(payment_id: str, amount: Decimal | None = None) -> dict:
    """Refund a payment in full, or `amount` (rupees). {id, amount, status}."""
    if not payment_id:
        raise PaymentError("no Razorpay payment to refund")
    body = {"amount": to_paise(amount)} if amount is not None else {}
    try:
        refund = await _api("POST", f"/payments/{payment_id}/refund", json_body=body)
    except IntegrationError as exc:
        log.error("razorpay_refund_failed", payment_id=payment_id, error=str(exc))
        raise PaymentError(f"Razorpay refund failed: {exc}") from exc
    refund = refund or {}
    log.info("razorpay_refund_created", refund_id=refund.get("id"), payment_id=payment_id)
    return {"id": refund.get("id"),
            "amount": Decimal(str(refund.get("amount") or 0)) / 100,
            "status": refund.get("status")}


# --- signatures ----------------------------------------------------------------
def _hmac(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_webhook(payload: bytes, signature: str | None) -> dict:
    """The webhook's event, once its X-Razorpay-Signature checks out:
    HMAC-SHA256 of the raw body with the webhook secret. Raises PaymentError."""
    secret = settings.razorpay_webhook_secret
    if is_unset(secret):
        raise PaymentError("RAZORPAY_WEBHOOK_SECRET is not configured")
    if not signature:
        raise PaymentError("missing X-Razorpay-Signature header")
    if not hmac.compare_digest(_hmac(secret, payload), signature.strip()):
        raise PaymentError("invalid Razorpay signature")
    try:
        event = json.loads(payload)
    except ValueError as exc:
        raise PaymentError(f"malformed Razorpay payload: {exc}") from exc
    if not isinstance(event, dict):
        raise PaymentError("unexpected Razorpay payload")
    return event


CALLBACK_FIELDS = ("razorpay_payment_link_id", "razorpay_payment_link_reference_id",
                   "razorpay_payment_link_status", "razorpay_payment_id")


def verify_callback(params) -> bool:
    """Is the redirect after paying really from Razorpay? Its signature is
    HMAC-SHA256, with the key secret, of link id | reference id | status |
    payment id."""
    secret = settings.razorpay_key_secret
    signature = str(params.get("razorpay_signature") or "")
    if is_unset(secret) or not signature:
        return False
    if not all(params.get(name) for name in CALLBACK_FIELDS):
        return False
    message = "|".join(str(params.get(name)) for name in CALLBACK_FIELDS).encode()
    return hmac.compare_digest(_hmac(secret, message), signature)
