"""Short payment redirect and the payment return pages.

Checkout URLs are far longer than a WhatsApp button allows, so the Pay Now
button points at `pay.<domain>/<order_number>` and this route 302s to the
live link (spec section 3 note) - a Stripe session for Shero US, a Razorpay
payment link for DST India (integrations/payment_gateway.py). DST India's
pages are the same routes under /dst.

Point the `pay` subdomain at this service with `/` rewritten to `/pay/`, or
set PAY_REDIRECT_BASE_URL to `https://<api-host>/pay` and skip the rewrite.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import locale
from app.core.logging import get_logger
from app.db.models import Customer, PaymentStatus
from app.db.session import get_session
from app.integrations import payment_gateway
from app.integrations.razorpay_gw import checkout as razorpay_checkout
from app.services import order_link
from app.services import orders as order_service
from app.services import payments
from app.services.slots import as_utc

log = get_logger(__name__)
router = APIRouter(prefix="/pay", tags=["payment"])

# Any business's order number: SHO-261116-K4T9P (Shero US), DST-... (DST India).
ORDER_NUMBER_RE = re.compile(r"^[A-Z]{2,5}-\d{6}-[0-9A-Z]{5}$")


@router.get("/success/{order_number}", response_class=HTMLResponse)
async def payment_success_page(
    order_number: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Where Stripe or Razorpay sends the customer after paying.

    Deliberately says nothing definitive about the order: the webhook is what
    confirms payment, and it may not have landed yet. Razorpay's return carries
    a signed outcome, so a paid one is confirmed here too (idempotently - the
    webhook then finds the order already paid, or got there first).
    """
    if payment_gateway.uses_razorpay() and request.query_params.get("razorpay_signature"):
        await _confirm_razorpay_return(session, order_number, request.query_params)
    return _page(
        "Payment received",
        "Thank you! Your payment is being confirmed.<br>"
        "We have sent the details to your WhatsApp.",
        order_number,
    )


@router.get("/cancelled/{order_number}", response_class=HTMLResponse)
async def payment_cancelled_page(order_number: str) -> HTMLResponse:
    return _page(
        "Payment cancelled",
        "No payment was taken. Your cart is still saved - "
        "head back to WhatsApp to finish your order.",
        order_number,
    )


async def _confirm_razorpay_return(session: AsyncSession, order_number: str,
                                   params) -> bool:
    """Mark the order paid from Razorpay's signed return, if it says paid.

    Trusted only when the signature checks out with the key secret and the
    link is the order's current one. Anything else is left to the webhook."""
    number = order_number.upper()
    if not razorpay_checkout.verify_callback(params):
        log.warning("razorpay_return_unverified", order_number=number)
        return False
    if str(params.get("razorpay_payment_link_status") or "").lower() != razorpay_checkout.PAID:
        return False
    if not ORDER_NUMBER_RE.match(number):
        return False
    order = await order_service.get_by_number(session, number)
    if order is None or order.stripe_session_id != params.get("razorpay_payment_link_id"):
        log.warning("razorpay_return_unknown_link", order_number=number)
        return False
    customer = await session.get(Customer, order.customer_id)
    if customer is None:
        log.error("razorpay_return_no_customer", order_number=number)
        return False
    try:
        paid = await payments.handle_payment_success(
            session, order, customer, payment_intent_id=params.get("razorpay_payment_id"))
    except Exception as exc:  # noqa: BLE001 - the webhook still confirms it
        await session.rollback()
        log.error("razorpay_return_confirm_failed", order_number=number, error=str(exc))
        return False
    log.info("razorpay_return_confirmed", order_number=number, newly_paid=paid)
    return paid


@router.get("/mock/{order_number}", response_class=HTMLResponse)
async def mock_checkout_page(
    order_number: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Stand-in for Stripe or Razorpay while no key is set (mock_mode)."""
    order = await _mock_order(session, order_number)
    if order is None:
        return _page("Not found", "We could not find that order.", "", status=404)
    if order.payment_status == PaymentStatus.PAID:
        return _page("Already paid", "This test order is already marked paid.",
                     order.order_number)
    # Simulating the payment runs exactly what the payment webhook would: slot
    # booked, Lead converted to a Contact, Order filed in the CRM, kitchen told.
    # The business's own prefix (/dst for DST India) keeps the post in it.
    root = html.escape(request.scope.get("root_path", ""))
    button = (
        f"<form method='post' action='{root}/pay/mock/{html.escape(order.order_number)}/paid'>"
        "<button style='margin-top:1.25rem;width:100%;padding:.8rem;border:0;"
        "border-radius:10px;background:#2F7549;color:#fff;font-weight:600;"
        "font-size:1rem;cursor:pointer'>Simulate successful payment</button></form>"
    )
    discount = getattr(order, "discount", None)
    saving = (f"Coupon {html.escape(order.promo_code or '')}: "
              f"<b>-{html.escape(locale.money(discount))}</b> included<br>"
              if discount else "")
    return _page(
        "Test payment",
        f"Amount: <b>{order.total:.2f} {html.escape(order.currency.upper())}</b><br>"
        + saving +
        "This is a demo checkout - no card is charged.<br>"
        f"Real payments start once {payment_gateway.name()} is connected." + button,
        order.order_number,
    )


@router.post("/mock/{order_number}/paid", response_class=HTMLResponse)
async def mock_payment_success(
    order_number: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Mark a test order paid, as the payment webhook would. Mock mode only."""
    if not payment_gateway.mock_mode():
        # With a real key only Stripe or Razorpay may say an order is paid.
        return _page("Not found", "That payment link is not valid.", "", status=404)
    order = await _mock_order(session, order_number)
    if order is None:
        return _page("Not found", "We could not find that order.", "", status=404)
    customer = await session.get(Customer, order.customer_id)
    if customer is None:
        log.error("mock_payment_no_customer", order_number=order.order_number)
        return _page("Not found", "We could not find that order.", "", status=404)

    await payments.handle_payment_success(session, order, customer)
    log.info("mock_payment_succeeded", order_number=order.order_number)
    return _page(
        "Payment simulated",
        "The order is now marked paid. The confirmation goes to WhatsApp, "
        "and the customer and order are sent to the CRM.",
        order.order_number,
    )


async def _mock_order(session: AsyncSession, order_number: str):
    """A test order by number, only while payments are mocked."""
    number = order_number.upper()
    if not ORDER_NUMBER_RE.match(number):
        return None
    order = await order_service.get_by_number(session, number)
    if order is None or not (order.stripe_session_id or "").startswith(
            payment_gateway.current().MOCK_SESSION_PREFIX):
        return None     # a real Stripe or Razorpay order is never paid from here
    return order


@router.get("/{order_number}")
async def redirect_to_checkout(
    order_number: str,
    session: AsyncSession = Depends(get_session),
):
    """Resolve the short link to the live checkout URL (Stripe or Razorpay)."""
    if not ORDER_NUMBER_RE.match(order_number.upper()):
        return _page("Not found", "That payment link is not valid.", "", status=404)

    order = await order_service.get_by_number(session, order_number.upper())
    if order is None or not order.checkout_url:
        log.info("pay_redirect_unknown_order", order_number=order_number)
        return _page("Not found", "We could not find that order.", "", status=404)

    if order.payment_status == PaymentStatus.PAID:
        return _page(
            "Already paid",
            "This order is already paid and confirmed. "
            "Check WhatsApp for your delivery slot.",
            order.order_number,
        )

    # as_utc: SQLite hands the stamp back without a timezone, and comparing
    # that with an aware "now" raises.
    expired = (
        order.payment_status in (PaymentStatus.EXPIRED, PaymentStatus.REFUNDED)
        or (order.payment_link_expires_at is not None
            and as_utc(order.payment_link_expires_at) <= datetime.now(timezone.utc))
    )
    if expired:
        # The payment_expired template's Order Now button lands here: send
        # the customer to the menu, where the order's dishes are waiting.
        menu = _fresh_menu_url(order.customer_id)
        if menu:
            log.info("pay_redirect_expired_to_menu", order_number=order.order_number)
            return RedirectResponse(menu, status_code=302)
        return _page(
            "Link expired",
            "This payment link has expired and the delivery slot was released. "
            "Message us on WhatsApp to start again.",
            order.order_number,
        )

    log.info("pay_redirect", order_number=order.order_number)
    return RedirectResponse(order.checkout_url, status_code=302)


def _fresh_menu_url(customer_id) -> str | None:
    """A new signed menu link, or None if links cannot be signed here."""
    try:
        return order_link.build_url(customer_id)
    except RuntimeError as exc:
        log.error("pay_redirect_no_menu_link", error=str(exc))
        return None


def _page(title: str, message: str, order_number: str,
          status: int = 200) -> HTMLResponse:
    """Minimal branded response page.

    The order number is HTML-escaped: it comes off the URL, so it is untrusted
    input even though the format is checked.
    """
    reference = (
        f"<p class='ref'>Order {html.escape(order_number)}</p>"
        if order_number else ""
    )
    body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} - Shero Home Food</title>
<link rel="icon" type="image/png" href="/static/brand/favicon-64.png">
<meta name="theme-color" content="#029B99">
<style>
  /* Shero brand colours, as in templates/_brand.html. */
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
         display: grid; place-items: center; min-height: 100vh; margin: 0;
         background: #EFF6F5; color: #0D1F1E; }}
  .card {{ background: #fff; padding: 2.5rem 2rem; border-radius: 16px;
          border-top: 4px solid #029B99;
          box-shadow: 0 10px 30px rgba(13,31,30,.08); max-width: 26rem;
          text-align: center; }}
  .logo {{ width: 72px; height: 72px; border-radius: 16px; margin: 0 auto 1rem;
          display: block; }}
  h1 {{ font-size: 1.35rem; margin: 0 0 .75rem; color: #026B69; }}
  p {{ line-height: 1.6; margin: 0; color: #25403F; }}
  .ref {{ margin-top: 1.25rem; font-size: .85rem; color: #5b7472; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #0D1F1E; color: #E6F5F5; }}
    .card {{ background: #172E2D; box-shadow: none; }}
    h1 {{ color: #5CC0BD; }}
    p {{ color: #C4E9E8; }}
  }}
</style>
</head>
<body>
  <div class="card">
    <img class="logo" src="/static/brand/shero-logo-192.png" alt="Shero Home Food">
    <h1>{html.escape(title)}</h1>
    <p>{message}</p>
    {reference}
  </div>
</body>
</html>"""
    return HTMLResponse(body, status_code=status)
