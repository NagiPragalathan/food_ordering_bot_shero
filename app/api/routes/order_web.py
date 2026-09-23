"""The web ordering page: browse, cart, address, slot, summary, pay.

A WhatsApp list holds ten rows, which makes a 287-dish menu a paging exercise
rather than a menu. So the bot sends a link instead: the customer picks dishes
on a real page, confirms an address and a slot, sees the quote, and the
payment link is sent back to WhatsApp - the spec's step 15, unchanged.

Every route re-reads the token, so the page holds no authority of its own, and
every price is recalculated server-side from the menu. The browser's cart is
a list of retailer ids and quantities; nothing it claims about money is
trusted.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (BotFlowError, ConfigurationError,
                                 IntegrationError, PaymentError)
from app.core.logging import get_logger
from app.db.models import ConversationStep, Customer, OrderStage
from app.db.session import get_session
from app.services import kitchen as kitchen_service
from app.services import menu as menu_service
from app.services import order_link, orders, payments, pricing, slots
from app.services.customers import get_or_create_conversation
from app.templating import templates

log = get_logger(__name__)
router = APIRouter(prefix="/order", tags=["ordering"])

# Shown when an upstream we depend on is unconfigured or down. It names
# nothing internal: the detail goes to the log, not the customer.
UPSTREAM_DOWN = ("We could not work out the delivery charge just now. "
                 "Please try again shortly, or message us on WhatsApp.")
PAYMENT_DOWN = ("We could not start the payment just now. Nothing has been "
                "charged - please try again shortly.")


async def _customer(session: AsyncSession, token: str) -> Customer | None:
    customer_id = order_link.read_token(token)
    if customer_id is None:
        return None
    return await session.get(Customer, customer_id)


def _expired(request: Request) -> HTMLResponse:
    """Shown when a link is old or tampered with."""
    return templates.TemplateResponse(request, "order/expired.html", status_code=410)


@router.get("/{token}", response_class=HTMLResponse)
async def storefront(request: Request, token: str,
                     session: AsyncSession = Depends(get_session)):
    """The page itself. Everything after this is JSON."""
    customer = await _customer(session, token)
    if customer is None:
        return _expired(request)

    kitchen = await kitchen_service.get_kitchen(session)
    return templates.TemplateResponse(request, "order/index.html", {
        "token": token,
        "customer": customer,
        "kitchen": kitchen,
        "currency_symbol": "$",
    })


@router.get("/{token}/menu")
async def menu_json(token: str, session: AsyncSession = Depends(get_session)) -> dict:
    """The whole live menu, grouped for the page to render at once."""
    customer = await _customer(session, token)
    if customer is None:
        return {"ok": False, "error": "This ordering link has expired."}

    cuisines = []
    for cuisine in await menu_service.list_cuisines(session):
        categories = []
        for category in await menu_service.list_categories(session, cuisine.slug):
            items = await menu_service.list_items(session, cuisine.slug,
                                                  category.slug)
            if not items:
                continue
            categories.append({
                "name": category.name,
                "slug": category.slug,
                "items": [{
                    "id": item.retailer_id,
                    "name": item.name,
                    "price": float(item.price),
                    "pack_size": item.pack_size or "",
                    "serves": item.serves or "",
                } for item in items],
            })
        if categories:
            cuisines.append({"name": cuisine.name, "slug": cuisine.slug,
                             "categories": categories})

    return {"ok": True, "cuisines": cuisines, "saved": _saved_address(customer)}


def _saved_address(customer: Customer) -> dict:
    """Pre-fill the form from whatever the bot already captured."""
    return {
        "address_line1": customer.address_line1 or "",
        "apartment_unit": customer.apartment_unit or "",
        "delivery_instructions": customer.delivery_instructions or "",
        "contact_number": customer.contact_number or customer.whatsapp_number,
        "postal_code": customer.postal_code or "",
        "name": customer.name or "",
        "email": customer.email or "",
    }


@router.post("/{token}/address")
async def check_address(request: Request, token: str,
                        session: AsyncSession = Depends(get_session)) -> dict:
    """Serviceability check, then the bookable slots (spec steps 10 and 13)."""
    customer = await _customer(session, token)
    if customer is None:
        return {"ok": False, "error": "This ordering link has expired."}

    body = await request.json()
    postal_code = str(body.get("postal_code") or "").strip()
    if not postal_code:
        return {"ok": False, "error": "Enter your ZIP code."}

    point = await kitchen_service.resolve_location(
        latitude=_as_float(body.get("latitude")),
        longitude=_as_float(body.get("longitude")),
        postal_code=postal_code,
    )
    check = await kitchen_service.check_service(session, point,
                                               postal_code=postal_code)
    if not check.is_serviceable:
        return {"ok": False, "serviceable": False,
                "error": check.reason or "We do not deliver to that area yet."}

    kitchen = await kitchen_service.get_kitchen(session)
    if kitchen is None:
        return {"ok": False, "error": "No kitchen is configured yet."}

    available = await slots.list_available_slots(session, kitchen)
    if not available:
        return {"ok": False, "error": "There are no delivery slots available "
                                      "right now. Please try again later."}

    # Remember the location so the bot does not re-ask on WhatsApp.
    if point is not None:
        customer.latitude, customer.longitude = point.latitude, point.longitude
    customer.postal_code = postal_code

    return {
        "ok": True,
        "serviceable": True,
        "distance_km": check.distance_km,
        "slots": [{"id": s.slot_id, "label": s.label} for s in available],
    }


@router.post("/{token}/quote")
async def quote(request: Request, token: str,
                session: AsyncSession = Depends(get_session)) -> dict:
    """Price the cart with delivery, for the summary screen (spec step 14)."""
    customer = await _customer(session, token)
    if customer is None:
        return {"ok": False, "error": "This ordering link has expired."}

    body = await request.json()
    cart = _clean_cart(body.get("cart"))
    if not cart:
        return {"ok": False, "error": "Your cart is empty."}

    kitchen = await kitchen_service.get_kitchen(session)
    if kitchen is None:
        return {"ok": False, "error": "No kitchen is configured yet."}

    slot = await slots.get_slot(session, str(body.get("slot_id") or ""))
    if slot is None:
        return {"ok": False, "error": "Choose a delivery slot."}

    try:
        priced = await pricing.price_cart(
            session, cart,
            outlet=kitchen,
            dropoff_latitude=customer.latitude,
            dropoff_longitude=customer.longitude,
            dropoff_address=_dropoff(body, customer),
            slot_starts_at=slots.as_utc(slot.starts_at),
        )
    except BotFlowError as exc:
        # Item unavailable, no delivery quote - all carry customer-safe copy.
        return {"ok": False, "error": exc.customer_message}
    except (ConfigurationError, IntegrationError) as exc:
        # A missing Uber or Stripe credential is an operator problem, not the
        # customer's. Say so plainly here instead of returning a 500.
        log.error("web_quote_failed", error=str(exc))
        return {"ok": False, "error": UPSTREAM_DOWN}

    return {
        "ok": True,
        "lines": [{"name": line.name, "quantity": line.quantity,
                   "line_total": float(line.line_total)} for line in priced.lines],
        "dish_total": float(priced.dish_total),
        "delivery_fee": float(priced.delivery_fee),
        "extra_fees": float(priced.extra_fees),
        "tax": float(priced.tax),
        "total": float(priced.total),
        "currency": priced.currency,
        "slot_label": slot.label(slots.outlet_tz(kitchen)),
    }


@router.post("/{token}/confirm")
async def confirm(request: Request, token: str,
                  session: AsyncSession = Depends(get_session)) -> dict:
    """Create the order, hold the slot, and send the payment link to WhatsApp."""
    customer = await _customer(session, token)
    if customer is None:
        return {"ok": False, "error": "This ordering link has expired."}

    body = await request.json()
    cart = _clean_cart(body.get("cart"))
    if not cart:
        return {"ok": False, "error": "Your cart is empty."}

    address = str(body.get("address_line1") or "").strip()
    if not address:
        return {"ok": False, "error": "Enter your delivery address."}

    kitchen = await kitchen_service.get_kitchen(session)
    if kitchen is None:
        return {"ok": False, "error": "No kitchen is configured yet."}

    slot = await slots.get_slot(session, str(body.get("slot_id") or ""))
    if slot is None:
        return {"ok": False, "error": "Choose a delivery slot."}

    # Save the details on the customer so WhatsApp and the CRM agree.
    customer.address_line1 = address
    customer.apartment_unit = str(body.get("apartment_unit") or "").strip() or None
    customer.delivery_instructions = (
        str(body.get("delivery_instructions") or "").strip() or None)
    customer.contact_number = (
        str(body.get("contact_number") or "").strip() or customer.whatsapp_number)

    draft = None   # set once the order exists, so cleanup knows what to undo
    try:
        priced = await pricing.price_cart(
            session, cart,
            outlet=kitchen,
            dropoff_latitude=customer.latitude,
            dropoff_longitude=customer.longitude,
            dropoff_address=_dropoff(body, customer),
            slot_starts_at=slots.as_utc(slot.starts_at),
        )
        order = await orders.create_draft_order(
            session, customer=customer, outlet=kitchen, priced=priced,
            slot_id=slot.id, slot_starts_at=slots.as_utc(slot.starts_at),
            slot_ends_at=slots.as_utc(slot.ends_at),
            slot_label=slot.label(slots.outlet_tz(kitchen)),
        )
        draft = order
        await slots.hold_slot(session, slot_id=slot.id, order_id=order.id)
        pay_url = await payments.create_payment_link(session, order, customer)
    except BotFlowError as exc:
        await _release(session, draft)
        return {"ok": False, "error": exc.customer_message}
    except (ConfigurationError, IntegrationError) as exc:
        log.error("web_confirm_failed", error=str(exc))
        await _release(session, draft)
        return {"ok": False, "error": UPSTREAM_DOWN}
    except PaymentError as exc:
        # The order exists but has no payment link, so do not leave its slot
        # reserved against an order nobody can pay for.
        log.error("web_payment_link_failed", error=str(exc))
        await _release(session, draft)
        return {"ok": False, "error": PAYMENT_DOWN}

    # Park the WhatsApp conversation on payment so the reminder and expiry
    # jobs treat a web order exactly like one placed in chat.
    conversation = await get_or_create_conversation(session, customer)
    conversation.step = ConversationStep.AWAIT_PAYMENT
    conversation.set(order_id=str(order.id))

    log.info("web_order_confirmed", order_number=order.order_number,
             customer_id=str(customer.id), total=str(order.total))

    return {"ok": True, "order_number": order.order_number, "pay_url": pay_url}


# --- helpers -----------------------------------------------------------------
async def _release(session: AsyncSession, order) -> None:
    """Give a slot back when the order that reserved it cannot be paid for."""
    if order is None:
        return
    try:
        await slots.release_holds_for_order(session, order.id,
                                            reason="payment_link_failed")
        orders.set_stage(order, OrderStage.CANCELLED)
        await session.flush()
    except Exception:   # never mask the original failure with a cleanup one
        log.exception("web_order_cleanup_failed",
                      order_number=getattr(order, "order_number", "?"))



def _clean_cart(raw) -> list[dict]:
    """Keep only well-formed lines; the browser can send anything."""
    if not isinstance(raw, list):
        return []
    cart = []
    for line in raw:
        if not isinstance(line, dict):
            continue
        retailer_id = str(line.get("retailer_id") or line.get("id") or "").strip()
        try:
            quantity = int(line.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        if retailer_id and 1 <= quantity <= 20:
            cart.append({"retailer_id": retailer_id, "quantity": quantity})
    return cart


def _dropoff(body: dict, customer: Customer) -> dict:
    return {
        "street": str(body.get("address_line1") or customer.address_line1 or ""),
        "unit": str(body.get("apartment_unit") or customer.apartment_unit or ""),
        "postal_code": str(body.get("postal_code") or customer.postal_code or ""),
    }


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
