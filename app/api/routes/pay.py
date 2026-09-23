"""Short payment redirect and the Stripe return pages.

Stripe checkout URLs are far longer than a WhatsApp button allows, so the
Pay Now button points at `pay.<domain>/<order_number>` and this route 302s to
the live session (spec section 3 note).

Point the `pay` subdomain at this service with `/` rewritten to `/pay/`, or
set PAY_REDIRECT_BASE_URL to `https://<api-host>/pay` and skip the rewrite.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import PaymentStatus
from app.db.session import get_session
from app.services import orders as order_service

log = get_logger(__name__)
router = APIRouter(prefix="/pay", tags=["payment"])

ORDER_NUMBER_RE = re.compile(r"^SHO-\d{6}-[0-9A-Z]{5}$")


@router.get("/success/{order_number}", response_class=HTMLResponse)
async def payment_success_page(order_number: str) -> HTMLResponse:
    """Where Stripe sends the customer after paying.

    Deliberately says nothing definitive about the order: the webhook is what
    confirms payment, and it may not have landed yet.
    """
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


@router.get("/{order_number}")
async def redirect_to_checkout(
    order_number: str,
    session: AsyncSession = Depends(get_session),
):
    """Resolve the short link to the live Stripe checkout URL."""
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

    expired = (
        order.payment_status in (PaymentStatus.EXPIRED, PaymentStatus.REFUNDED)
        or (order.payment_link_expires_at is not None
            and order.payment_link_expires_at <= datetime.now(timezone.utc))
    )
    if expired:
        return _page(
            "Link expired",
            "This payment link has expired and the delivery slot was released. "
            "Message us on WhatsApp to start again.",
            order.order_number,
        )

    log.info("pay_redirect", order_number=order.order_number)
    return RedirectResponse(order.checkout_url, status_code=302)


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
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
         display: grid; place-items: center; min-height: 100vh; margin: 0;
         background: #faf7f2; color: #241c16; }}
  .card {{ background: #fff; padding: 2.5rem 2rem; border-radius: 16px;
          box-shadow: 0 10px 30px rgba(0,0,0,.08); max-width: 26rem;
          text-align: center; }}
  h1 {{ font-size: 1.35rem; margin: 0 0 .75rem; }}
  p {{ line-height: 1.6; margin: 0; color: #5a4c42; }}
  .ref {{ margin-top: 1.25rem; font-size: .85rem; color: #9a8a7c; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #17120f; color: #f3ece6; }}
    .card {{ background: #221b17; box-shadow: none; }}
    p {{ color: #c8b8aa; }}
  }}
</style>
</head>
<body>
  <div class="card">
    <h1>{html.escape(title)}</h1>
    <p>{message}</p>
    {reference}
  </div>
</body>
</html>"""
    return HTMLResponse(body, status_code=status)
