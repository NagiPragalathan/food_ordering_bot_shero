"""Booking the Uber courier for a paid order.

Orders are placed at least a day ahead (slots.earliest_bookable), so the
courier is not booked at payment time: an Uber booking made a day early
would expire. A job runs every few minutes and books each paid order whose
slot starts within UBER_DISPATCH_HOURS_BEFORE (2 hours by default). The
courier may collect from then on and must deliver inside the slot:

    pickup   from now (booking time)      until the slot ends
    dropoff  from the slot start           until the slot ends

A failure (Uber down, credentials missing, an address Uber rejects) is kept
on the order and logged, and the next run tries again until the slot is
over, so a short outage does not lose a delivery. Nothing here is shown to
the customer; the kitchen's Out for Delivery step still drives their
WhatsApp updates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ConfigurationError, IntegrationError
from app.core.logging import get_logger
from app.db.models import Customer, DeliverySlot, Order, OrderStage, Outlet, PaymentStatus
from app.integrations.uber import direct as uber
from app.integrations.zoho import fields as f
from app.services.slots import as_utc

log = get_logger(__name__)

# Stages at which a paid order still needs its courier.
AWAITING_COURIER = (str(OrderStage.PAID_SLOT_BOOKED), str(OrderStage.SENT_TO_KITCHEN))


def due_time(slot_starts_at: datetime | None) -> datetime | None:
    """When an order is sent to Uber: UBER_DISPATCH_HOURS_BEFORE its slot."""
    if slot_starts_at is None:
        return None
    return as_utc(slot_starts_at) - timedelta(hours=settings.uber_dispatch_hours_before)


async def due_orders(session: AsyncSession, now: datetime | None = None) -> list[Order]:
    """Queued orders whose send time has come and whose slot has not ended.

    The send time is stored at payment; an order paid before that existed
    falls back to its slot start minus the dispatch window.
    """
    now = now or datetime.now(timezone.utc)
    window_end = now + timedelta(hours=settings.uber_dispatch_hours_before)
    rows = await session.execute(
        select(Order)
        .join(DeliverySlot, DeliverySlot.id == Order.slot_id)
        .where(Order.payment_status == str(PaymentStatus.PAID),
               Order.stage.in_(AWAITING_COURIER),
               Order.uber_delivery_id.is_(None),
               or_(Order.uber_dispatch_due_at <= now,
                   Order.uber_dispatch_due_at.is_(None) & (DeliverySlot.starts_at <= window_end)),
               DeliverySlot.ends_at > now)
        .order_by(DeliverySlot.starts_at)
    )
    return list(rows.scalars())


# --- the admin's Uber queue ----------------------------------------------------
WAITING, BOOKED, RETRYING, MISSED, CANCELLED = (
    "Waiting", "Booked", "Retrying", "Missed", "Cancelled")


@dataclass
class QueueRow:
    order: Order
    customer: Customer | None
    slot_start: datetime | None
    slot_end: datetime | None
    send_at: datetime | None
    status: str


async def queue(session: AsyncSession, now: datetime | None = None,
                limit: int = 100) -> list[QueueRow]:
    """Every paid order the courier matters for, soonest slot first: those
    still to deliver, and those whose slot ended in the last two days."""
    now = now or datetime.now(timezone.utc)
    rows = await session.execute(
        select(Order, DeliverySlot, Customer)
        .join(DeliverySlot, DeliverySlot.id == Order.slot_id)
        .join(Customer, Customer.id == Order.customer_id)
        .where(Order.payment_status == str(PaymentStatus.PAID),
               DeliverySlot.ends_at > now - timedelta(days=2))
        .order_by(DeliverySlot.starts_at)
        .limit(limit)
    )
    out = []
    for order, slot, customer in rows.all():
        start, end = as_utc(slot.starts_at), as_utc(slot.ends_at)
        out.append(QueueRow(order=order, customer=customer, slot_start=start, slot_end=end,
                            send_at=as_utc(order.uber_dispatch_due_at) if order.uber_dispatch_due_at
                            else due_time(start),
                            status=_status(order, end, now)))
    return out


def _status(order: Order, slot_end: datetime, now: datetime) -> str:
    if order.uber_delivery_id:
        return CANCELLED if (order.uber_delivery_status or "") == "canceled" else BOOKED
    if slot_end <= now:
        return MISSED
    return RETRYING if order.uber_dispatch_error else WAITING


async def cancel(order: Order) -> bool:
    """Cancel the booked courier (a test booking, or a cancelled order)."""
    if not order.uber_delivery_id:
        return False
    try:
        status = await uber.cancel_delivery(order.uber_delivery_id)
    except (IntegrationError, ConfigurationError) as exc:
        return _failed(order, f"cancel failed: {exc}")
    order.uber_delivery_status = status or "canceled"
    log.info("uber_delivery_cancelled", order_number=order.order_number,
             delivery_id=order.uber_delivery_id)
    return True


async def dispatch(session: AsyncSession, order: Order, *,
                   now: datetime | None = None) -> bool:
    """Book the courier for one order. True when Uber accepted it."""
    now = now or datetime.now(timezone.utc)
    slot = await session.get(DeliverySlot, order.slot_id) if order.slot_id else None
    outlet = await session.get(Outlet, order.outlet_id) if order.outlet_id else None
    customer = await session.get(Customer, order.customer_id)
    problem = _missing(order, slot, outlet, customer)
    if problem:
        return _failed(order, problem)
    assert slot and outlet and customer          # checked by _missing

    starts, ends = as_utc(slot.starts_at), as_utc(slot.ends_at)
    try:
        delivery = await uber.create_delivery(
            pickup=uber.build_address(street=outlet.address_line1, city=outlet.city,
                                      state=outlet.state, zip_code=outlet.postal_code,
                                      country=outlet.country or "US"),
            pickup_name=outlet.name,
            pickup_phone=_e164(outlet.phone or outlet.kitchen_whatsapp),
            pickup_latitude=outlet.latitude, pickup_longitude=outlet.longitude,
            dropoff=_dropoff_address(order, outlet),
            dropoff_name=customer.name or "Shero customer",
            dropoff_phone=_e164(order.contact_number or customer.whatsapp_number),
            dropoff_latitude=_float(order.delivery_latitude),
            dropoff_longitude=_float(order.delivery_longitude),
            items=_manifest(order),
            external_id=order.order_number,
            # Pickup opens at the send time, not at the moment of booking:
            # an early "Send now" for tomorrow would otherwise ask Uber for a
            # day-long pickup window, which it caps and then refuses.
            pickup_ready_at=max(now, due_time(starts) or now), pickup_deadline_at=ends,
            dropoff_ready_at=max(now, starts), dropoff_deadline_at=ends,
            dropoff_notes=order.delivery_instructions,
        )
    except (IntegrationError, ConfigurationError) as exc:
        return _failed(order, str(exc))

    order.uber_delivery_id = delivery.delivery_id
    order.uber_delivery_status = delivery.status or None
    order.uber_tracking_url = delivery.tracking_url
    order.uber_dispatched_at = now
    order.uber_dispatch_error = None
    log.info("uber_delivery_booked", order_number=order.order_number,
             delivery_id=delivery.delivery_id, slot_start=starts.isoformat())
    return True


# --- helpers -----------------------------------------------------------------
def _missing(order: Order, slot, outlet, customer) -> str | None:
    """Why this order cannot be sent to Uber, or None."""
    if slot is None:
        return "the order has no delivery slot"
    if outlet is None:
        return "the order has no kitchen"
    if customer is None:
        return "the customer no longer exists"
    if not (outlet.phone or outlet.kitchen_whatsapp):
        return "the kitchen has no phone number for the courier (Kitchens page)"
    if not order.delivery_address:
        return "the order has no delivery address"
    return None


def _failed(order: Order, reason: str) -> bool:
    order.uber_dispatch_error = reason[:500]
    log.error("uber_delivery_failed", order_number=order.order_number, error=reason)
    return False


def _dropoff_address(order: Order, outlet: Outlet) -> dict:
    """The delivery address. Orders store street, unit and ZIP; the city and
    state are the kitchen's, as every delivery is local to it."""
    address = uber.build_address(street=order.delivery_address or "", city=outlet.city,
                                 state=outlet.state, zip_code=order.postal_code or "",
                                 country=outlet.country or "US")
    if order.apartment_unit:
        address["street_address"].append(order.apartment_unit)
    return address


def _manifest(order: Order) -> list[dict]:
    items = []
    for line in order.items or []:
        parsed = f.parse_line(line)
        if parsed:
            items.append({"name": parsed[1][:100], "quantity": parsed[2], "size": "small"})
    return items or [{"name": f"Shero order {order.order_number}", "quantity": 1,
                      "size": "small"}]


def _e164(number: str | None) -> str:
    digits = "".join(ch for ch in number or "" if ch.isdigit())
    return f"+{digits}" if digits else ""


def _float(value) -> float | None:
    return None if value is None else float(value)


# --- the Uber queue page's filters ------------------------------------------------
STATUSES = (WAITING, BOOKED, RETRYING, MISSED, CANCELLED)
WHEN = {"today": "Today", "upcoming": "Upcoming", "past": "Past"}


def matches(row: QueueRow, *, query: str = "", when: str = "", now: datetime,
            tz: tzinfo = timezone.utc) -> bool:
    """Does this queue row pass the page's search and date filter?

    The search looks at the order number, the customer's name and number,
    and the delivery address and ZIP. "Today" is the kitchen's today.
    """
    if query:
        needle = query.strip().lower().lstrip("+")
        customer = row.customer
        haystack = " ".join(str(part or "") for part in (
            row.order.order_number, row.order.delivery_address, row.order.postal_code,
            customer.name if customer else "", customer.whatsapp_number if customer else "",
        )).lower()
        if needle not in haystack:
            return False
    if when == "today":
        return row.slot_start is not None and row.slot_start.astimezone(tz).date() == \
            now.astimezone(tz).date()
    if when == "upcoming":
        return row.slot_end is not None and row.slot_end > now
    if when == "past":
        return row.slot_end is not None and row.slot_end <= now
    return True


def status_counts(rows: list[QueueRow]) -> dict[str, int]:
    """How many rows have each status (for the page's tabs)."""
    return {status: sum(1 for r in rows if r.status == status) for status in STATUSES}
