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
from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import ConversationStep, Customer, LeadStage, OrderStage
from app.db.session import get_session
from app.services import addresses, crm_sync, media_thumbs
from app.services import cart as cart_service
from app.services import kitchen as kitchen_service
from app.services import menu as menu_service
from app.services import order_link, orders, payments, pricing, slots
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
    return await order_link.customer_for_token(session, token)


def _expired(request: Request) -> HTMLResponse:
    """Shown when a link is old or tampered with."""
    return templates.TemplateResponse(request, "order/expired.html", {
        "new_link_url": order_link.new_link_whatsapp_url(),
    }, status_code=410)


@router.get("/{token}", response_class=HTMLResponse)
async def storefront(request: Request, token: str,
                     session: AsyncSession = Depends(get_session)):
    """The menu page. Everything after this is JSON."""
    return await _page(request, token, session, "order/index.html")


@router.get("/{token}/location", response_class=HTMLResponse)
async def location_page(request: Request, token: str,
                        session: AsyncSession = Depends(get_session)):
    """Address-only page for the order summary's Update location button.

    The dishes stay; the customer picks an address, a time and confirms, on
    the same JSON routes the menu page uses.
    """
    return await _page(request, token, session, "order/location.html")


async def _page(request: Request, token: str, session: AsyncSession,
                template: str) -> HTMLResponse:
    customer = await _customer(session, token)
    if customer is None:
        return _expired(request)

    kitchen = await kitchen_service.get_kitchen(session)
    return templates.TemplateResponse(request, template, {
        "token": token,
        "customer": customer,
        "kitchen": kitchen,
        "currency_symbol": "$",
        # Public by design; see google_maps_browser_key in config.
        "maps_browser_key": settings.google_maps_browser_key,
    })


@router.get("/{token}/menu")
async def menu_json(token: str, session: AsyncSession = Depends(get_session)) -> dict:
    """The whole live menu, grouped for the page to render at once."""
    customer = await _customer(session, token)
    if customer is None:
        return order_link.dead_link_response()

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
                    # Empty for every row in the client's sheet today; the
                    # page falls back to a drawn placeholder rather than a
                    # broken image.
                    "image_url": item.image_url or "",
                    # Card-sized copy for the list and the cart; image_url
                    # stays the full photo for the dish's own sheet.
                    "thumb_url": media_thumbs.thumb_url(item.image_url),
                    "description": item.description or "",
                    "category": category.name,
                    "cuisine": cuisine.name,
                } for item in items],
            })
        if categories:
            cuisines.append({"name": cuisine.name, "slug": cuisine.slug,
                             "categories": categories})

    # Addresses are served by order_addresses.py, not bundled with the menu.
    return {"ok": True, "cuisines": cuisines}


@router.get("/{token}/cart")
async def read_cart(token: str, session: AsyncSession = Depends(get_session)) -> dict:
    """The saved cart, so reopening the link picks up where it left off."""
    customer = await _customer(session, token)
    if customer is None:
        return order_link.dead_link_response()

    _, lines = await cart_service.for_customer(session, customer)
    return _cart_response(lines)


@router.post("/{token}/cart")
async def update_cart(request: Request, token: str,
                      session: AsyncSession = Depends(get_session)) -> dict:
    """Set one dish to an exact quantity. 0 removes it.

    Exact rather than incremental so a double-tap or a retried request cannot
    quietly add a dish twice.
    """
    customer = await _customer(session, token)
    if customer is None:
        return order_link.dead_link_response()

    body = await request.json()
    retailer_id = str(body.get("retailer_id") or "").strip()
    if not retailer_id:
        return {"ok": False, "error": "No dish was given."}

    try:
        quantity = int(body.get("quantity"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "Quantity must be a number."}

    conversation, _ = await cart_service.for_customer(session, customer)

    if quantity <= 0:
        lines = cart_service.set_quantity(conversation, retailer_id=retailer_id,
                                          name="", price="0", quantity=0)
        return _cart_response(lines)

    # Name and price come from the menu, never from the browser. The lookup
    # excludes unavailable dishes, so this also stops one being re-added
    # after the kitchen hides it.
    found = await menu_service.get_by_retailer_ids(session, [retailer_id])
    item = found.get(retailer_id)
    if item is None:
        return {"ok": False, "error": "That dish is no longer available."}

    lines = cart_service.set_quantity(conversation, retailer_id=retailer_id,
                                      name=item.name, price=item.price,
                                      quantity=quantity)
    await crm_sync.advance_stage(customer, LeadStage.CART_CREATED, forward_only=True)
    await _note_cuisine(session, customer, retailer_id)
    return _cart_response(lines)


@router.post("/{token}/cart/clear")
async def clear_cart(token: str,
                     session: AsyncSession = Depends(get_session)) -> dict:
    customer = await _customer(session, token)
    if customer is None:
        return order_link.dead_link_response()

    conversation, _ = await cart_service.for_customer(session, customer)
    cart_service.clear(conversation)
    return _cart_response([])


async def _note_cuisine(session: AsyncSession, customer: Customer,
                        retailer_id: str) -> None:
    """The web menu has no cuisine step, so the dishes say which it is.

    Written to Zoho only when it changes, not on every tap of +.
    """
    cuisine = await menu_service.cuisine_slug_for(session, retailer_id)
    if cuisine and cuisine != customer.cuisine_preference:
        customer.cuisine_preference = cuisine
        await crm_sync.push_details(customer, cuisine=cuisine)


def _cart_response(lines: list[dict]) -> dict:
    return {
        "ok": True,
        "cart": lines,
        "count": cart_service.item_count(lines),
        "subtotal": float(cart_service.subtotal(lines)),
    }


@router.post("/{token}/address")
async def check_address(request: Request, token: str,
                        session: AsyncSession = Depends(get_session)) -> dict:
    """Serviceability check, then the bookable slots (spec steps 10 and 13)."""
    customer = await _customer(session, token)
    if customer is None:
        return order_link.dead_link_response()

    body = await request.json()
    try:
        address = await addresses.get_owned(session, customer, body.get("address_id"))
    except addresses.AddressError as exc:
        return {"ok": False, "error": str(exc)}

    # A map pin is exact; a typed address falls back to its ZIP.
    point = await kitchen_service.resolve_location(
        latitude=address.latitude,
        longitude=address.longitude,
        postal_code=address.postal_code,
    )
    check = await kitchen_service.check_service(session, point,
                                               postal_code=address.postal_code)
    if not check.is_serviceable:
        await crm_sync.advance_stage(customer, LeadStage.NOT_SERVICEABLE)
        return {"ok": False, "serviceable": False,
                "error": check.reason or "We do not deliver to that area yet."}

    kitchen = await kitchen_service.get_kitchen(session)
    if kitchen is None:
        return {"ok": False, "error": "No kitchen is configured yet."}

    available = await slots.list_available_slots(session, kitchen)
    if not available:
        return {"ok": False, "error": "There are no delivery slots available "
                                      "right now. Please try again later."}

    # Remember the address so the bot does not re-ask on WhatsApp, and so it
    # is the one pre-selected next time.
    await addresses.use_for_order(session, customer, address)
    if point is not None:
        customer.latitude, customer.longitude = point.latitude, point.longitude

    await crm_sync.advance_stage(customer, LeadStage.OUTLET_SELECTED, forward_only=True)
    await crm_sync.push_details(
        customer, **addresses.crm_details(address), outlet_name=kitchen.name,
        distance_km=check.distance_km,
    )

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
        return order_link.dead_link_response()

    body = await request.json()
    # The cart is read from the database, not from the request: the page is
    # only a view of it, and a tab left open for an hour must not price a
    # cart the customer has since changed somewhere else.
    _, stored = await cart_service.for_customer(session, customer)
    cart = _clean_cart(stored)
    if not cart:
        return {"ok": False, "error": "Your cart is empty."}

    kitchen = await kitchen_service.get_kitchen(session)
    if kitchen is None:
        return {"ok": False, "error": "No kitchen is configured yet."}

    slot = await slots.get_slot(session, str(body.get("slot_id") or ""))
    if slot is None:
        return {"ok": False, "error": "Choose a delivery slot."}

    try:
        address = await addresses.get_owned(session, customer, body.get("address_id"))
    except addresses.AddressError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        priced = await pricing.price_cart(
            session, cart,
            outlet=kitchen,
            dropoff_latitude=customer.latitude,
            dropoff_longitude=customer.longitude,
            dropoff_address=_dropoff(address),
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

    await crm_sync.advance_stage(customer, LeadStage.SLOT_SELECTED, forward_only=True)
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
        return order_link.dead_link_response()

    body = await request.json()
    conversation, stored = await cart_service.for_customer(session, customer)
    cart = _clean_cart(stored)
    if not cart:
        return {"ok": False, "error": "Your cart is empty."}

    try:
        address = await addresses.get_owned(session, customer, body.get("address_id"))
    except addresses.AddressError as exc:
        return {"ok": False, "error": str(exc)}

    kitchen = await kitchen_service.get_kitchen(session)
    if kitchen is None:
        return {"ok": False, "error": "No kitchen is configured yet."}

    slot = await slots.get_slot(session, str(body.get("slot_id") or ""))
    if slot is None:
        return {"ok": False, "error": "Choose a delivery slot."}

    # Copy the chosen address onto the customer so WhatsApp and the CRM
    # agree. The contact number is their WhatsApp number, never page input.
    await addresses.use_for_order(session, customer, address)

    draft = None   # set once the order exists, so cleanup knows what to undo
    try:
        priced = await pricing.price_cart(
            session, cart,
            outlet=kitchen,
            dropoff_latitude=customer.latitude,
            dropoff_longitude=customer.longitude,
            dropoff_address=_dropoff(address),
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
        # The full summary, with Change menu and Update location, goes to
        # WhatsApp; the page then hands the customer over to the chat.
        pay_url = await payments.create_payment_link(session, order, customer,
                                                     with_summary=True)
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
    # jobs treat a web order exactly like one placed in chat. The cart is
    # emptied now it has become an order.
    conversation.step = ConversationStep.AWAIT_PAYMENT
    conversation.set(order_id=str(order.id))
    cart_service.clear(conversation)

    log.info("web_order_confirmed", order_number=order.order_number,
             customer_id=str(customer.id), total=str(order.total))

    return {"ok": True, "order_number": order.order_number, "pay_url": pay_url,
            "whatsapp_url": order_link.whatsapp_chat_url()}


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


def _dropoff(address) -> dict:
    return {
        "street": address.address_line1,
        "unit": address.apartment_unit or "",
        "postal_code": address.postal_code,
    }
