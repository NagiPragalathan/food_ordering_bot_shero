"""Telling the kitchen about a paid order (spec step 17), on its delivery day.

Orders are placed at least a day ahead (slots.earliest_bookable), so a
kitchen alerted at payment would get tomorrow's orders mixed with today's.
Instead each paid order is assigned to the kitchen chosen for its address
(services/kitchen.check_service) and queued. A job sends the alert at
KITCHEN_ALERT_HOUR, kitchen local time, on the delivery day:

    paid Mon 15:00, slot Tue 18:00  ->  alert Tue 07:00

The alert is never later than the Uber booking (dispatch.due_time), so the
kitchen always hears before the courier is sent. An order paid after its
alert time is alerted on the next run.

OPEN QUESTION: no approved template exists for the kitchen alert, and a
message to a kitchen that has not messaged us in 24 hours falls outside
WhatsApp's window, so the text send lands only while the kitchen's chat is
open. The order is marked Sent to Kitchen and visible in the admin either way.
"""

from __future__ import annotations

from datetime import datetime, time, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Customer, DeliverySlot, Order, OrderStage, Outlet, PaymentStatus
from app.integrations.gallabox.sender import current_sender
from app.services import crm_sync, dispatch, orders
from app.services.slots import as_utc, outlet_tz

log = get_logger(__name__)


def notify_time(slot_starts_at: datetime | None, outlet: Outlet | None) -> datetime | None:
    """When the kitchen hears about an order delivered at `slot_starts_at`."""
    if slot_starts_at is None:
        return None
    start = as_utc(slot_starts_at)
    tz = outlet_tz(outlet) if outlet is not None else timezone.utc
    local_day = start.astimezone(tz).date()
    hour = settings.kitchen_alert_hour
    at = time(int(hour), min(59, round((hour % 1) * 60)))
    morning = datetime.combine(local_day, at, tzinfo=tz).astimezone(timezone.utc)
    latest = dispatch.due_time(start) or start
    return min(morning, latest)


async def due_alerts(session: AsyncSession, now: datetime | None = None) -> list[Order]:
    """Paid orders whose kitchen alert time has come and that were not sent."""
    now = now or datetime.now(timezone.utc)
    rows = await session.execute(
        select(Order)
        .join(DeliverySlot, DeliverySlot.id == Order.slot_id)
        .where(Order.payment_status == str(PaymentStatus.PAID),
               Order.stage == str(OrderStage.PAID_SLOT_BOOKED),
               Order.kitchen_notified_at.is_(None),
               Order.kitchen_notify_at <= now,
               DeliverySlot.ends_at > now)
        .order_by(DeliverySlot.starts_at)
    )
    return list(rows.scalars())


async def send_alert(session: AsyncSession, order: Order, *,
                     now: datetime | None = None) -> bool:
    """Mark the order Sent to Kitchen and message that kitchen.

    True when the WhatsApp message went out. The stage moves either way, so
    one kitchen with no number set does not keep the order in the queue.
    """
    now = now or datetime.now(timezone.utc)
    outlet = await session.get(Outlet, order.outlet_id) if order.outlet_id else None
    customer = await session.get(Customer, order.customer_id)

    orders.set_stage(order, OrderStage.SENT_TO_KITCHEN)
    order.kitchen_notified_at = now
    await session.flush()
    await crm_sync.push_order_stage(order, OrderStage.SENT_TO_KITCHEN)

    if not (outlet and outlet.kitchen_whatsapp):
        log.warning("kitchen_alert_skipped", order_number=order.order_number,
                    outlet=outlet.code if outlet else None,
                    reason="no kitchen WhatsApp number configured")
        return False

    try:
        await current_sender().send_text(outlet.kitchen_whatsapp, message(order, customer))
    except Exception as exc:  # noqa: BLE001 - the order is paid; never fail it on this
        log.error("kitchen_alert_failed", order_number=order.order_number,
                  outlet=outlet.code, error=str(exc))
        return False
    log.info("kitchen_alerted", order_number=order.order_number, outlet=outlet.code)
    return True


def message(order: Order, customer: Customer | None) -> str:
    lines = [
        f"New paid order {order.order_number}",
        f"Slot: {order.slot_label or 'not set'}",
        "",
        "Items:",
    ]
    for item in order.items or []:
        lines.append(f"  {item.get('quantity')} x {item.get('name')}")
    contact = order.contact_number or (customer.whatsapp_number if customer else "")
    lines += [
        "",
        f"Total: {order.total:.2f} {order.currency}",
        f"Deliver to: {order.delivery_address or ''} {order.apartment_unit or ''}".strip(),
        f"Contact: {contact}",
    ]
    if order.delivery_instructions:
        lines.append(f"Notes: {order.delivery_instructions}")
    return "\n".join(lines)

