"""Payment orchestration (spec steps 15-17).

Covers the whole money path: create the Stripe link, send it on the approved
template, chase it at 15 minutes, expire it at 30 and release the slot, then
react to whatever Stripe reports.

Every handler here is idempotent. Stripe retries webhooks and may deliver the
same event more than once, so each one checks the current state first and
returns early if the transition has already happened.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import IntegrationError, PaymentError
from app.core.logging import get_logger
from app.db.models import (
    ConversationStep,
    Customer,
    LeadStage,
    Order,
    OrderStage,
    Outlet,
    PaymentStatus,
)
from app.integrations.gallabox import template_status
from app.integrations.gallabox import templates as tpl
from app.integrations.gallabox.sender import current_sender
from app.integrations.stripe_gw import checkout
from app.services import crm_sync, dispatch, orders, receipts, slots
from app.services.customers import advance, get_or_create_conversation

log = get_logger(__name__)


async def send_payment_confirmation(customer: Customer, order: Order,
                                    outlet_name: str) -> bool:
    """Tell the customer their payment went through (spec step 16), with a
    Download Bill button. If the template is not approved the client sends
    the same words as an ordinary message (gallabox/fallback.py). Returns
    True when a message was accepted."""
    try:
        await current_sender().send_template(
            customer.whatsapp_number, tpl.PAYMENT_SUCCESS, order.order_number,
            f"{order.total:.2f}", outlet_name, order.slot_label or "your chosen slot",
            button_value=receipts.build_token(order.order_number))
        return True
    except IntegrationError as exc:
        log.error("payment_success_message_failed", order_number=order.order_number,
                  error=str(exc))
        return False


def payment_short_url(order_number: str) -> str:
    """Short redirect used on the Pay Now button.

    Stripe checkout URLs are far too long for a WhatsApp button, so the button
    points here and this service 302s to the real session (spec section 3 note).
    """
    return f"{settings.pay_redirect_base_url.rstrip('/')}/{order_number}"


# --- creating the link -------------------------------------------------------
async def create_payment_link(session: AsyncSession, order: Order,
                              customer: Customer, *, with_summary: bool = False) -> str:
    """Create the Stripe session, store it, and send the payment template.

    `with_summary` (the web page) sends the full order summary with Pay Now,
    Change menu and Update location instead of the short payment_link.
    """
    result = await checkout.create_checkout_session(
        order_id=str(order.id),
        order_number=order.order_number,
        items=order.items or [],
        delivery_fee=Decimal(order.delivery_fee),
        taxes_and_fees=Decimal(order.extra_fees) + Decimal(order.tax),
        currency=order.currency,
        customer_email=customer.email,
        customer_phone=customer.whatsapp_number,
    )

    order.stripe_session_id = result["id"]
    order.checkout_url = result["url"]
    order.payment_link_expires_at = result["expires_at"]
    order.payment_status = PaymentStatus.LINK_SENT
    await session.flush()

    if with_summary:
        await send_order_summary(order, customer)
    else:
        await _send_payment_link(order, customer)

    await crm_sync.advance_stage(customer, LeadStage.PAYMENT_LINK_SENT)
    log.info("payment_link_sent", order_number=order.order_number)
    return result["url"]


async def _send_payment_link(order: Order, customer: Customer) -> None:
    await current_sender().send_template(
        customer.whatsapp_number,
        tpl.PAYMENT_LINK,
        customer.greeting_name,
        order.order_number,
        f"{order.total:.2f}",
        order.slot_label or "your chosen slot",
        button_value=order.order_number,
    )


async def send_order_summary(order: Order, customer: Customer) -> None:
    """The order_summary template, or payment_link while it is not approved.

    The customer must always get a way to pay. Approval is checked first,
    because Gallabox accepts a send for an unapproved template and it then
    fails silently; a send error falls back too. Either way the fallback is
    the approved payment_link - losing Change menu and Update location, not
    the order.
    """
    if not await template_status.is_approved(tpl.ORDER_SUMMARY.name):
        log.info("order_summary_not_approved_using_payment_link",
                 order_number=order.order_number)
        await _send_payment_link(order, customer)
        return
    try:
        await current_sender().send_template(
            customer.whatsapp_number,
            tpl.ORDER_SUMMARY,
            customer.greeting_name,
            order.order_number,
            summary_items(order.items or []),
            summary_address(order),
            order.slot_label or "your chosen slot",
            f"{order.total:.2f}",
            button_value=order.order_number,
        )
    except IntegrationError as exc:
        log.warning("order_summary_template_failed", order_number=order.order_number,
                    error=str(exc))
        await _send_payment_link(order, customer)


# Meta caps a template parameter's length and refuses line breaks in one.
SUMMARY_ITEMS_LIMIT = 400


def summary_items(items: list[dict]) -> str:
    """ "2 x Drumstick Sambar, 1 x Beans Sambar" - one line, capped."""
    parts = [f"{int(line.get('quantity') or 1)} x {line.get('name') or 'Item'}"
             for line in items]
    text = ""
    for index, part in enumerate(parts):
        candidate = f"{text}, {part}" if text else part
        if len(candidate) > SUMMARY_ITEMS_LIMIT:
            return f"{text} and {len(parts) - index} more"
        text = candidate
    return " ".join(text.split()) or "your items"


def summary_address(order: Order) -> str:
    """Street, unit and ZIP on one line."""
    parts = [order.delivery_address, order.apartment_unit, order.postal_code]
    return " ".join(", ".join(p.strip() for p in parts if p and p.strip()).split()) \
        or "your saved address"


async def send_reminder(session: AsyncSession, order: Order,
                        customer: Customer) -> bool:
    """15-minute unpaid nudge (spec step 15).

    Switched off by default (`SEND_PAYMENT_REMINDER`). The guard is here, at
    the single place the reminder is sent, so no caller can route around it.
    Expiry is unaffected: an unpaid link still dies at 30 minutes and still
    releases its slot.
    """
    if not settings.send_payment_reminder:
        return False
    if order.payment_status != PaymentStatus.LINK_SENT or order.reminder_sent_at:
        return False

    await current_sender().send_template(
        customer.whatsapp_number,
        tpl.PAYMENT_REMINDER,
        customer.greeting_name,
        order.order_number,
        order.slot_label or "your chosen slot",
        button_value=order.order_number,
    )
    order.reminder_sent_at = datetime.now(timezone.utc)
    await session.flush()
    log.info("payment_reminder_sent", order_number=order.order_number)
    return True


# --- webhook outcomes --------------------------------------------------------
async def handle_payment_success(session: AsyncSession, order: Order,
                                 customer: Customer,
                                 payment_intent_id: str | None = None) -> bool:
    """Payment confirmed: book the slot, convert the lead, tell everyone.

    Idempotent - a duplicate `checkout.session.completed` returns False without
    double-booking the slot or filing a second Zoho order.
    """
    if order.payment_status == PaymentStatus.PAID:
        log.info("payment_success_duplicate", order_number=order.order_number)
        return False

    if payment_intent_id:
        order.stripe_payment_intent_id = payment_intent_id
    order.payment_status = PaymentStatus.PAID
    order.paid_at = datetime.now(timezone.utc)

    # 1. Convert the held slot into a booking (spec step 16).
    await slots.book_slot(session, order.id)
    orders.set_stage(order, OrderStage.PAID_SLOT_BOOKED)
    # The chat was parked waiting for this payment. Close it, or the next
    # message the customer sends is told their payment link is still open.
    conversation = await get_or_create_conversation(session, customer)
    if conversation.step == str(ConversationStep.AWAIT_PAYMENT):
        advance(conversation, ConversationStep.COMPLETED)
    await session.flush()

    ctx = await crm_sync.order_context(session, order)
    outlet, outlet_name = ctx.outlet, ctx.outlet_name or "Shero"
    # Into the Uber queue: sent UBER_DISPATCH_HOURS_BEFORE the slot starts.
    order.uber_dispatch_due_at = dispatch.due_time(ctx.delivery_at)

    # 2. Confirmation to the customer. The money is taken by now, so a
    # WhatsApp failure must not stop the CRM record and the kitchen alert.
    await send_payment_confirmation(customer, order, outlet_name)

    # 3. Lead -> Contact, the Order record and its Order Items (spec step 16).
    await crm_sync.convert_and_record_order(customer, order, ctx)

    # 4. Kitchen alert (spec step 17).
    await notify_kitchen(session, order, outlet, customer)

    log.info("payment_success", order_number=order.order_number,
             total=str(order.total))
    return True


async def handle_payment_failed(session: AsyncSession, order: Order,
                                customer: Customer) -> bool:
    """Stripe reported a failure: offer a retry link (spec step 16)."""
    if order.payment_status in (PaymentStatus.PAID, PaymentStatus.FAILED):
        return False

    order.payment_status = PaymentStatus.FAILED
    await session.flush()

    await current_sender().send_template(
        customer.whatsapp_number,
        tpl.PAYMENT_FAILED,
        customer.greeting_name,
        order.order_number,
        button_value=order.order_number,
    )
    await crm_sync.advance_stage(customer, LeadStage.PAYMENT_FAILED)
    log.info("payment_failed", order_number=order.order_number)
    return True


async def handle_payment_expired(session: AsyncSession, order: Order,
                                 customer: Customer) -> bool:
    """Link lapsed unpaid: release the slot and re-engage (spec step 15)."""
    if order.payment_status in (PaymentStatus.PAID, PaymentStatus.EXPIRED):
        return False

    order.payment_status = PaymentStatus.EXPIRED
    # The whole point of the 30-minute expiry: give the window back.
    await slots.release_holds_for_order(session, order.id, reason="payment_expired")
    await _restore_cart(session, order, customer)
    await session.flush()

    await current_sender().send_template(
        customer.whatsapp_number,
        tpl.PAYMENT_EXPIRED,
        customer.greeting_name,
        button_value=order.order_number,
    )
    await crm_sync.advance_stage(customer, LeadStage.PAYMENT_ABANDONED)
    log.info("payment_expired", order_number=order.order_number)
    return True


async def _restore_cart(session: AsyncSession, order: Order,
                        customer: Customer) -> None:
    """Put an expired order's dishes back in the cart, unless it has new ones.

    The payment_expired message promises "your cart is still saved", and its
    Order Now button opens the menu on that cart.
    """
    from app.services import cart as cart_service
    from app.services.order_changes import as_cart

    conversation, lines = await cart_service.for_customer(session, customer)
    if not lines:
        conversation.set(cart=as_cart(order.items or []))


async def refund_order(session: AsyncSession, order: Order, customer: Customer,
                       amount: Decimal | None = None) -> bool:
    """Refund and notify (order stage Cancelled / Refunded)."""
    if not order.stripe_payment_intent_id:
        raise PaymentError(f"order {order.order_number} has no payment to refund")
    if order.payment_status == PaymentStatus.REFUNDED:
        return False

    refund = await checkout.create_refund(order.stripe_payment_intent_id, amount)

    order.payment_status = PaymentStatus.REFUNDED
    order.stripe_refund_id = refund["id"]
    order.refund_amount = Decimal(refund["amount"])
    orders.set_stage(order, OrderStage.REFUNDED)
    await slots.release_holds_for_order(session, order.id, reason="refunded")
    await session.flush()

    await current_sender().send_template(
        customer.whatsapp_number,
        tpl.REFUND_PROCESSED,
        f"{order.refund_amount:.2f}",
        order.order_number,
    )
    await crm_sync.push_order_stage(order, OrderStage.REFUNDED)
    log.info("order_refunded", order_number=order.order_number,
             amount=str(order.refund_amount))
    return True


# --- kitchen -----------------------------------------------------------------
async def notify_kitchen(session: AsyncSession, order: Order,
                         outlet: Outlet | None, customer: Customer) -> None:
    """Tell the outlet about a new paid order (spec step 17).

    OPEN QUESTION: the spec lists no approved template for this, and a message
    to a kitchen that has not messaged us first falls outside the 24-hour
    window, so a plain text send will only land if the outlet has an open
    session. Until the client decides (dedicated template, email, or the ops
    dashboard alone), the order is always marked Sent to Kitchen and visible
    on the ops API - the WhatsApp ping is best-effort on top.
    """
    orders.set_stage(order, OrderStage.SENT_TO_KITCHEN)
    await session.flush()
    await crm_sync.push_order_stage(order, OrderStage.SENT_TO_KITCHEN)

    if not (outlet and outlet.kitchen_whatsapp):
        log.info("kitchen_alert_skipped", order_number=order.order_number,
                 reason="no kitchen WhatsApp number configured")
        return

    body = _kitchen_message(order, customer)
    try:
        await current_sender().send_text(outlet.kitchen_whatsapp, body)
        log.info("kitchen_alerted", order_number=order.order_number,
                 outlet=outlet.code)
    except Exception as exc:  # noqa: BLE001 - never fail a paid order on this
        log.error("kitchen_alert_failed", order_number=order.order_number,
                  error=str(exc))


def _kitchen_message(order: Order, customer: Customer) -> str:
    lines = [
        f"New paid order {order.order_number}",
        f"Slot: {order.slot_label or 'not set'}",
        "",
        "Items:",
    ]
    for item in order.items or []:
        lines.append(f"  {item.get('quantity')} x {item.get('name')}")
    lines += [
        "",
        f"Total: {order.total:.2f} {order.currency}",
        f"Deliver to: {order.delivery_address or ''} {order.apartment_unit or ''}".strip(),
        f"Contact: {order.contact_number or customer.whatsapp_number}",
    ]
    if order.delivery_instructions:
        lines.append(f"Notes: {order.delivery_instructions}")
    return "\n".join(lines)
