"""Notify the assigned kitchen immediately after payment; retry failed sends.

The kitchen partner gets the order code, delivery time, kitchen and dishes
(`shero_kitchen_order`) and nothing about the customer: no name, number or
address, and nothing the customer paid. Shero arranges the delivery. The
team's alert (services/order_alerts.py) still carries the full details.

Each dish goes with its quantity and Triple-P (PPP, what Shero pays the
partner for it), then the order's PPP total - never the MRP:

    2 x Poondu (Garlic) Rasam - 450 ml (PPP ₹44.00 each, ₹88.00);
    1 x Vazhaikkai Podimas - 450 ml (PPP ₹68.00); Total PPP: ₹156.00

The PPP is the one the order was priced with (`unit_ppp` on its lines, from
services/pricing.py); an order from before that gets the menu's PPP today.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import locale
from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import DeliverySlot, MenuItem, Order, OrderStage, Outlet, PaymentStatus
from app.integrations.gallabox import template_status
from app.integrations.gallabox import templates as tpl
from app.integrations.gallabox.sender import current_sender
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

    if not (outlet and outlet.kitchen_whatsapp):
        log.warning("kitchen_alert_skipped", order_number=order.order_number,
                    outlet=outlet.code if outlet else None,
                    reason="no kitchen WhatsApp number configured")
        return False

    try:
        # Every dish, a large cart split between whole dishes over messages.
        chunks = sections(await partner_items(session, order))
        spec = await template()
        for chunk in chunks:
            await current_sender().send_template(
                outlet.kitchen_whatsapp, spec, *values(spec, order, outlet.name, chunk))
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


async def template() -> tpl.TemplateSpec:
    """KITCHEN_ORDER; until Meta approves it, the approved team alert with the
    customer's details blanked (`values`), so the kitchen still hears about
    the order outside WhatsApp's 24-hour window."""
    if (not await template_status.is_approved(tpl.KITCHEN_ORDER.name)
            and await template_status.is_approved(tpl.ORDER_ALERT.name)):
        return tpl.ORDER_ALERT
    return tpl.KITCHEN_ORDER


def values(spec: tpl.TemplateSpec, order: Order, kitchen_name: str,
           items: str) -> tuple[str, ...]:
    """The kitchen's message: order code, delivery time, kitchen and dishes.
    On the team template, the customer, amount and address are "-"."""
    number, delivery, kitchen = (order_alerts._line(v) for v in
                                 (order.order_number, order.slot_label, kitchen_name))
    items = order_alerts._line(items)
    if spec is tpl.ORDER_ALERT:
        return number, "-", "-", delivery, kitchen, "-", items
    return number, delivery, kitchen, items


async def partner_items(session: AsyncSession, order: Order) -> list[str]:
    """One text per dish - quantity, name and PPP - then the PPP total."""
    lines = [line for line in order.items or [] if isinstance(line, dict)]
    missing = [str(line.get("retailer_id")) for line in lines
               if line.get("unit_ppp") in (None, "") and line.get("retailer_id")]
    menu = dict((await session.execute(
        select(MenuItem.retailer_id, MenuItem.cost_price)
        .where(MenuItem.retailer_id.in_(missing)))).all()) if missing else {}
    texts, total, unpriced = [], Decimal("0"), 0
    for line in lines:
        try:
            quantity = max(int(line.get("quantity") or 1), 1)
        except (TypeError, ValueError):
            quantity = 1
        name = " ".join(str(line.get("name") or line.get("retailer_id") or "?").split())
        ppp = _decimal(line.get("unit_ppp"))
        if ppp is None:
            ppp = _decimal(menu.get(str(line.get("retailer_id"))))
        if ppp is None:
            unpriced += 1
            texts.append(f"{quantity} x {name} (PPP not set)")
            continue
        total += ppp * quantity
        each = locale.money(ppp)
        texts.append(f"{quantity} x {name} (PPP {each})" if quantity == 1 else
                     f"{quantity} x {name} (PPP {each} each, {locale.money(ppp * quantity)})")
    if not texts:
        return ["-"]
    if unpriced == len(texts):
        return texts
    summary = f"Total PPP: {locale.money(total)}"
    if unpriced:
        summary += f", plus {unpriced} dish{'' if unpriced == 1 else 'es'} without a PPP"
    return texts + [summary]


def sections(texts: list[str]) -> list[str]:
    """The texts joined by "; " into as few template values as fit, never
    splitting a dish (a price cut in two reads wrong); "(1/2)" when several."""
    room = order_alerts.MAX_VALUE - len("(10/10) ")
    chunks: list[str] = []
    for text in texts:
        text = text if len(text) <= room else text[:room - 1] + "…"
        if chunks and len(chunks[-1]) + 2 + len(text) <= room:
            chunks[-1] += "; " + text
        else:
            chunks.append(text)
    if len(chunks) > 1:
        chunks = [f"({i}/{len(chunks)}) {chunk}" for i, chunk in enumerate(chunks, 1)]
    return chunks


def _decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except (ArithmeticError, ValueError):
        return None
