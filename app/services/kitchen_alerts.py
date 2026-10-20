"""Notify the assigned kitchen immediately after payment; retry failed sends."""

from __future__ import annotations

from datetime import datetime, time, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Customer, DeliverySlot, Order, OrderStage, Outlet, PaymentStatus
from app.integrations.gallabox.sender import current_sender
from app.integrations.gallabox import templates as tpl
from app.services import order_alerts
from app.services import crm_sync, dispatch, orders
from app.services.slots import as_utc, outlet_tz

log = get_logger(__name__)


def notify_time(slot_starts_at: datetime | None, outlet: Outlet | None) -> datetime | None:
    """Legacy delivery-day schedule, retained for existing queued orders."""
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
        .outerjoin(DeliverySlot, DeliverySlot.id == Order.slot_id)
        .where(Order.payment_status == str(PaymentStatus.PAID),
               Order.stage == str(OrderStage.PAID_SLOT_BOOKED),
               Order.kitchen_notified_at.is_(None),
               Order.kitchen_notify_at <= now,
               or_(DeliverySlot.ends_at > now,
                   (DeliverySlot.id.is_(None) & (Order.slot_ends_at.is_(None) | (Order.slot_ends_at > now)))))
        .order_by(DeliverySlot.starts_at)
    )
    return list(rows.scalars())


async def send_alert(session: AsyncSession, order: Order, *,
                     now: datetime | None = None) -> bool:
    """Send once; only record success after the provider accepts the message.

    Failures stay pending for the worker or an admin retry. Acceptance by the
    provider is not a delivery/read receipt from the kitchen member.
    """
    # Serialize the payment callback, worker and manual retry on PostgreSQL.
    # A crash after provider acceptance can still require an at-least-once retry.
    locked = (await session.execute(select(Order).where(Order.id == order.id)
              .with_for_update().execution_options(populate_existing=True))).scalar_one()
    order = locked
    if order.kitchen_notified_at:
        return True
    if (order.payment_status != PaymentStatus.PAID
            or order.stage != OrderStage.PAID_SLOT_BOOKED):
        return False
    now = now or datetime.now(timezone.utc)
    outlet = await session.get(Outlet, order.outlet_id) if order.outlet_id else None
    customer = await session.get(Customer, order.customer_id)

    if not (outlet and outlet.kitchen_whatsapp):
        log.warning("kitchen_alert_skipped", order_number=order.order_number,
                    outlet=outlet.code if outlet else None,
                    reason="no kitchen WhatsApp number configured")
        return False

    try:
        params = order_alerts.values(order, customer, outlet.name)
        # Send every item, splitting large carts into template-sized sections.
        raw = " ".join(order_alerts._items(order).split()) or "-"
        chunks = [raw[i:i + order_alerts.MAX_VALUE]
                  for i in range(0, len(raw), order_alerts.MAX_VALUE)]
        for chunk in chunks:
            await current_sender().send_template(
                outlet.kitchen_whatsapp, tpl.ORDER_ALERT, *params[:-1], chunk)

    except Exception as exc:  # noqa: BLE001 - the order is paid; never fail it on this
        log.error("kitchen_alert_failed", order_number=order.order_number,
                  outlet=outlet.code, error=str(exc))
        return False
    orders.set_stage(order, OrderStage.SENT_TO_KITCHEN)
    order.kitchen_notified_at = now
    await session.flush()
    await crm_sync.push_order_stage(order, OrderStage.SENT_TO_KITCHEN)
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

