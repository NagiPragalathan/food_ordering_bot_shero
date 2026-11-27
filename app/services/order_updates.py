"""The customer's WhatsApp at three stages of a paid order, and the invoice.

    Food Ready       Mark ready (Shiprocket) / Food ready     order_food_ready
    Food Dispatched  Out for delivery                         order_out_for_delivery
    Order Delivered  Mark delivered, with the invoice         order_delivered_v2

The invoice is made and sent only once the order is delivered (the client:
"Generate and send invoices only after order delivery, not upon order
placement"). The payment confirmation no longer carries it, and the receipt
link opens only for a delivered order (api/routes/receipts.py).

The Delivered message carries the invoice:
  * inside WhatsApp's 24-hour window (the customer wrote in the last 23
    hours): the invoice PDF itself, the message as its caption - one message,
    the invoice attached;
  * outside it, where WhatsApp takes only approved templates: the template
    with its Download Invoice button;
  * until Meta approves that template: the first Delivered template, without
    the invoice (the customer still hears it was delivered; the log says so).

All three follow SEND_DELIVERY_UPDATES (Settings). The stages move either
way; only the customer's message is skipped. A message that cannot go is
logged, never raised: the stage has already changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Customer, InboundMessage, Order, OrderStage, Outlet, PaymentStatus
from app.integrations.gallabox import template_status
from app.integrations.gallabox import templates as tpl
from app.integrations.gallabox.sender import current_sender
from app.services import receipts

log = get_logger(__name__)

# WhatsApp's window is 24 hours from the customer's last message; an hour
# spare, so a send near the edge goes as the template rather than be dropped.
WINDOW = timedelta(hours=23)
READY_STAGES = (OrderStage.SENT_TO_KITCHEN,)


@dataclass
class Result:
    ok: bool
    message: str


# --- the three messages ------------------------------------------------------------
async def notify_ready(session: AsyncSession, order: Order) -> bool:
    """Food Ready: "Your order #… is ready at <kitchen> and will be on its way"."""
    customer = await _customer(session, order)
    if customer is None:
        return False
    outlet = await session.get(Outlet, order.outlet_id) if order.outlet_id else None
    return await _send(order, "ready", lambda s: s.send_template(
        customer.whatsapp_number, tpl.ORDER_FOOD_READY, order.order_number,
        outlet.name if outlet else "our kitchen"))


async def notify_dispatched(session: AsyncSession, order: Order) -> bool:
    """Food Dispatched: "Your order #… is on its way!"."""
    customer = await _customer(session, order)
    if customer is None:
        return False
    return await _send(order, "dispatched", lambda s: s.send_template(
        customer.whatsapp_number, tpl.ORDER_OUT_FOR_DELIVERY, order.order_number))


async def notify_delivered(session: AsyncSession, order: Order) -> bool:
    """Order Delivered, with the invoice (see the module docstring)."""
    customer = await _customer(session, order)
    if customer is None:
        return False
    number = customer.whatsapp_number
    if await in_window(session, number):
        # WhatsApp fetches the invoice from its link straight away, from
        # another connection: the delivery must be committed by then, or the
        # link would still answer "not delivered yet".
        await session.commit()
        return await _send(order, "delivered_with_invoice_pdf", lambda s: s.send_document(
            number, url=receipts.receipt_url(order.order_number),
            filename=receipts.filename(order), caption=delivered_caption(order)))
    if (await template_status.is_approved(tpl.ORDER_DELIVERED.name)
            or not await template_status.is_approved(tpl.ORDER_DELIVERED_V1.name)):
        return await _send(order, "delivered_with_invoice_button", lambda s: s.send_template(
            number, tpl.ORDER_DELIVERED, order.order_number,
            button_value=receipts.build_token(order.order_number)))
    log.warning("invoice_waits_for_template", order_number=order.order_number,
                template=tpl.ORDER_DELIVERED.name)
    return await _send(order, "delivered_without_invoice", lambda s: s.send_template(
        number, tpl.ORDER_DELIVERED_V1, order.order_number))


async def notify_stage(session: AsyncSession, order: Order, stage: OrderStage) -> bool:
    """The message for a stage the order just moved to, if it has one."""
    if stage is OrderStage.OUT_FOR_DELIVERY:
        return await notify_dispatched(session, order)
    if stage is OrderStage.DELIVERED:
        return await notify_delivered(session, order)
    return False


def delivered_caption(order: Order) -> str:
    return (f"Your order #{order.order_number} has been delivered. Enjoy your meal! "
            "Your invoice is attached.")


# --- Food ready ------------------------------------------------------------------------
def awaits_ready(order: Order) -> bool:
    """The order's next step is Mark ready / Food ready: the pages show that
    one button, not Out for delivery beside it."""
    from app.services import shiprocket_booking
    if shiprocket_booking.enabled():
        return shiprocket_booking.awaits_rider(order)
    return (order.payment_status == PaymentStatus.PAID
            and order.stage in {str(s) for s in READY_STAGES}
            and order.ready_at is None)


async def mark_ready(session: AsyncSession, order: Order) -> Result:
    """The food is prepared. With Shiprocket connected (DST India) this also
    books its rider (shiprocket_booking.mark_ready, which records the time
    and tells the customer); otherwise it records the time and tells them."""
    from app.services import shiprocket_booking
    if shiprocket_booking.enabled():
        result = await shiprocket_booking.mark_ready(session, order)
        return Result(result.ok, result.message)
    number = order.order_number
    if order.payment_status != PaymentStatus.PAID:
        return Result(False, f"{number} is not paid.")
    if order.ready_at is not None:
        return Result(True, f"{number} is already marked ready.")
    if order.stage not in {str(s) for s in READY_STAGES}:
        return Result(False, f"{number} is {order.stage}: only an order with the kitchen "
                             "can be marked ready.")
    told = await record_ready(session, order)
    return Result(True, f"{number} is ready" + (
        "; the customer was told on WhatsApp." if told else
        "." if not settings.send_delivery_updates else
        ", but the customer's WhatsApp message could not be sent."))


async def record_ready(session: AsyncSession, order: Order) -> bool:
    """Record the food as ready, once, and tell the customer. True when the
    customer was messaged."""
    if order.ready_at is not None:
        return False
    order.ready_at = datetime.now(timezone.utc)
    await session.flush()
    log.info("order_food_ready", order_number=order.order_number)
    return await notify_ready(session, order)


# --- helpers ---------------------------------------------------------------------------
async def in_window(session: AsyncSession, whatsapp_number: str,
                    now: datetime | None = None) -> bool:
    """The customer wrote to us within WhatsApp's 24 hours (less WINDOW's
    margin), so an ordinary message - a document - reaches them."""
    last = await session.scalar(select(func.max(InboundMessage.created_at))
                                .where(InboundMessage.whatsapp_number == whatsapp_number))
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - last < WINDOW


async def _customer(session: AsyncSession, order: Order) -> Customer | None:
    if not settings.send_delivery_updates:
        return None
    return await session.get(Customer, order.customer_id) if order.customer_id else None


async def _send(order: Order, what: str, send) -> bool:
    try:
        await send(current_sender())
    except Exception as exc:  # noqa: BLE001 - the stage has changed; never fail it on this
        log.error("customer_stage_message_failed", order_number=order.order_number,
                  message=what, error=str(exc))
        return False
    log.info("customer_stage_message_sent", order_number=order.order_number, message=what)
    return True
