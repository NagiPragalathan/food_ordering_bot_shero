"""Mark ready: book a Shiprocket rider for a prepared order (own delivery).

When the food is prepared, staff press Mark ready on the Delivery queue or
the Orders page. The order is created on Shiprocket as a Hyperlocal
shipment - from the kitchen's Shiprocket pickup location to the customer's
map pin, prepaid - and a rider is assigned to it, the cheapest Shiprocket
offers. What came back (Shiprocket's ids, the courier, its tracking number
and link, the status, any error) is kept on the order's `courier`, so a
second press never books twice and a failed one can simply be pressed again.

Cancel booking cancels the Shiprocket order (and its rider); the order can
then be marked ready again, which creates a new Shiprocket order. Cancelling
the order itself (ops API) cancels its Shiprocket booking too.

Out for delivery and Delivered stay with the staff, as before. Without a
Shiprocket API user (DST_SHIPROCKET_EMAIL / _PASSWORD) nothing here runs and
the business's own riders deliver.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Customer, Order, OrderStage, Outlet, PaymentStatus
from app.integrations.shiprocket import client as shiprocket
from app.integrations.shiprocket.client import ShiprocketError
from app.services.slots import outlet_tz

log = get_logger(__name__)

PROVIDER = "shiprocket"
# The stages an order can be marked ready in: paid, with (or on its way to) the kitchen.
READY_STAGES = (OrderStage.PAID_SLOT_BOOKED, OrderStage.SENT_TO_KITCHEN)
# One food order's box, in cm (Shiprocket asks for every shipment's size).
BOX_CM = (25, 25, 10)
# Shiprocket wants a state's name; a kitchen may have been saved with its code.
STATES = {
    "AP": "Andhra Pradesh", "KA": "Karnataka", "KL": "Kerala", "TN": "Tamil Nadu",
    "TS": "Telangana", "TG": "Telangana", "MH": "Maharashtra", "DL": "Delhi",
    "GJ": "Gujarat", "WB": "West Bengal", "UP": "Uttar Pradesh", "HR": "Haryana",
    "PY": "Puducherry", "GA": "Goa", "RJ": "Rajasthan", "MP": "Madhya Pradesh",
}


@dataclass(frozen=True)
class Result:
    ok: bool
    message: str


def enabled() -> bool:
    """Mark ready books riders: own delivery, with a Shiprocket API user."""
    return settings.delivery_provider == "own" and shiprocket.configured()


def details(order: Order) -> dict:
    return dict(order.courier or {})


def booked(order: Order) -> bool:
    """A rider is booked (or being assigned) for this order."""
    return bool(details(order).get("assigned"))


def can_mark_ready(order: Order) -> bool:
    return (order.payment_status == PaymentStatus.PAID
            and order.stage in {str(s) for s in READY_STAGES}
            and not booked(order))


def can_cancel(order: Order) -> bool:
    """There is a Shiprocket order to cancel, and the food is not delivered."""
    return bool(details(order).get("order_id")) and order.stage != str(OrderStage.DELIVERED)


def shiprocket_ref(order: Order) -> str:
    """The order's id on Shiprocket: its number, then -2, -3 ... after a
    cancelled booking (Shiprocket keeps the cancelled one under the number)."""
    tries = len(details(order).get("cancelled") or [])
    return order.order_number if not tries else f"{order.order_number}-{tries + 1}"


def _save(order: Order, **changes) -> None:
    # A new dict, so the JSON column is seen to change.
    data = details(order)
    data.update(changes)
    order.courier = data


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _money(value) -> float:
    return float(Decimal(str(value or 0)).quantize(Decimal("0.01")))


def order_payload(order: Order, outlet: Outlet, customer: Customer | None, *,
                  now: datetime | None = None, ref: str | None = None) -> dict:
    """The order as Shiprocket's Create Custom Order takes it, for a
    Hyperlocal (HL) prepaid drop at the customer's map pin."""
    now = now or datetime.now(timezone.utc)
    name = ((customer.name if customer else None)
            or (customer.whatsapp_profile_name if customer else None) or "Customer").strip()
    first, _, last = name.partition(" ")
    phone = _digits(order.contact_number or (customer.whatsapp_number if customer else ""))[-10:]
    items = []
    for line in order.items or []:
        quantity = int(line.get("quantity") or 0)
        if quantity < 1:
            continue
        label = str(line.get("name") or line.get("retailer_id") or "Dish")
        items.append({
            "name": label[:200],
            "sku": str(line.get("retailer_id") or label)[:50],
            "units": quantity,
            "selling_price": _money(line.get("unit_price")),
        })
    delivery = _money(Decimal(str(order.delivery_fee or 0)) + Decimal(str(order.extra_fees or 0)))
    state = (outlet.state or "").strip()
    length, breadth, height = BOX_CM
    return {
        "order_id": ref or order.order_number,
        "order_date": now.astimezone(outlet_tz(outlet)).strftime("%Y-%m-%d %H:%M"),
        "pickup_location": outlet.shiprocket_pickup,
        "billing_customer_name": first[:60],
        "billing_last_name": last[:60],
        "billing_address": (order.delivery_address or "")[:200],
        "billing_address_2": ", ".join(p for p in (order.apartment_unit, order.delivery_instructions) if p)[:200],
        # A hyperlocal drop is in the kitchen's own city.
        "billing_city": (outlet.city or "")[:30],
        "billing_pincode": order.postal_code,
        "billing_state": STATES.get(state.upper(), state),
        "billing_country": "India",
        "billing_email": (customer.email if customer and customer.email else settings.shiprocket_email.strip()),
        "billing_phone": phone,
        "shipping_is_billing": True,
        "order_items": items,
        "payment_method": "Prepaid",
        "shipping_charges": delivery,
        "total_discount": _money(order.discount),
        # After deductions, before the delivery charge: Shiprocket adds that.
        "sub_total": _money(Decimal(str(order.total or 0)) - Decimal(str(delivery))),
        "length": length, "breadth": breadth, "height": height,
        "weight": settings.shiprocket_weight_kg,
        "latitude": order.delivery_latitude,
        "longitude": order.delivery_longitude,
        "shipping_method": "HL",
    }


async def mark_ready(session: AsyncSession, order: Order) -> Result:
    """Book a Shiprocket rider for this order (see the module docstring)."""
    number = order.order_number
    if not enabled():
        return Result(False, "Shiprocket is not connected yet, so no rider was booked.")
    if order.payment_status != PaymentStatus.PAID:
        return Result(False, f"{number} is not paid.")
    if booked(order):
        return Result(True, f"{number} already has a Shiprocket rider booked.")
    if order.stage not in {str(s) for s in READY_STAGES}:
        return Result(False, f"{number} is {order.stage}: only an order with the kitchen can be marked ready.")
    outlet = await session.get(Outlet, order.outlet_id) if order.outlet_id else None
    if outlet is None or not (outlet.shiprocket_pickup or "").strip():
        return Result(False, "Set this kitchen's Shiprocket pickup location on the Kitchens page "
                             "(the name it has in Shiprocket > Settings > Pickup Addresses).")
    if order.delivery_latitude is None or order.delivery_longitude is None:
        return Result(False, f"{number} has no map pin for the drop, which Shiprocket needs.")
    customer = await session.get(Customer, order.customer_id)

    now = datetime.now(timezone.utc)
    if order.ready_at is None:
        order.ready_at = now
    try:
        info = details(order)
        if not info.get("shipment_id"):
            created = await shiprocket.create_order(
                order_payload(order, outlet, customer, now=now, ref=shiprocket_ref(order)))
            if not created.get("shipment_id"):
                raise ShiprocketError("no shipment came back: "
                                      + str(created.get("message") or created)[:200])
            _save(order, provider=PROVIDER, order_id=str(created.get("order_id") or ""),
                  shipment_id=str(created["shipment_id"]), status=str(created.get("status") or "NEW"),
                  created_at=now.isoformat(), error=None)
            await session.flush()
        shipment_id = details(order)["shipment_id"]

        # The cheapest rider Shiprocket offers; without an answer, its default one.
        # Shiprocket Quick comes without a courier id: Shiprocket then assigns it.
        options = None
        try:
            options = await shiprocket.hyperlocal_rates(
                pickup_postcode=outlet.postal_code, delivery_postcode=order.postal_code or "",
                lat_from=outlet.latitude, long_from=outlet.longitude,
                lat_to=order.delivery_latitude, long_to=order.delivery_longitude,
                weight=settings.shiprocket_weight_kg)
        except ShiprocketError as exc:
            log.warning("shiprocket_rates_failed", order_number=number, error=exc.message)
        if options == []:
            raise ShiprocketError("no rider is available for this drop right now")
        choice = (options or [None])[0]

        assigned = shiprocket.assignment(
            await shiprocket.assign_rider(shipment_id, (choice or {}).get("id")))
        if assigned["error"]:
            raise ShiprocketError(assigned["error"])
    except ShiprocketError as exc:
        reason = exc.message
        _save(order, provider=PROVIDER, error=reason[:300], failed_at=now.isoformat())
        await session.flush()
        log.error("shiprocket_booking_failed", order_number=number, error=reason)
        return Result(False, f"Shiprocket could not book a rider for {number}: {reason}")

    awb = assigned["awb"]
    _save(order, assigned=True, awb=awb, courier_name=assigned["courier"] or (choice or {}).get("name"),
          fee=(choice or {}).get("rate"), booked_at=now.isoformat(), error=None,
          status="Rider assigned" if awb else "Assigning a rider",
          tracking_url=f"https://shiprocket.co/tracking/{awb}" if awb else None)
    await session.flush()
    log.info("shiprocket_rider_booked", order_number=number, awb=awb,
             courier=details(order).get("courier_name"))
    who = details(order).get("courier_name")
    return Result(True, f"{number} is ready: a rider is booked on Shiprocket"
                        + (f" ({who}, AWB {awb})." if awb else ". Shiprocket is assigning one now."))


async def cancel_booking(session: AsyncSession, order: Order) -> Result:
    """Cancel the order's Shiprocket order and its rider. What was booked is
    kept under `cancelled`; Mark ready can then book afresh."""
    number = order.order_number
    info = details(order)
    if not info.get("order_id"):
        if info.get("shipment_id"):
            return Result(False, f"{number}: cancel it on Shiprocket's dashboard "
                                 f"(shipment {info['shipment_id']}).")
        return Result(False, f"{number} has no Shiprocket booking to cancel.")
    if order.stage == str(OrderStage.DELIVERED):
        return Result(False, f"{number} is delivered: there is nothing to cancel.")
    try:
        answer = await shiprocket.cancel_order(info["order_id"])
    except ShiprocketError as exc:
        log.error("shiprocket_cancel_failed", order_number=number, error=exc.message)
        return Result(False, f"Shiprocket could not cancel {number}: {exc.message}")
    now = datetime.now(timezone.utc).isoformat()
    history = list(info.get("cancelled") or [])
    history.append({k: info.get(k) for k in ("order_id", "shipment_id", "awb", "courier_name", "fee")}
                   | {"at": now})
    _save(order, cancelled=history, assigned=False, order_id=None, shipment_id=None, awb=None,
          courier_name=None, fee=None, tracking_url=None, rider=None, eta=None, error=None,
          status="Cancelled on Shiprocket", cancelled_at=now)
    await session.flush()
    log.info("shiprocket_booking_cancelled", order_number=number, order_id=history[-1]["order_id"],
             answer=str(answer.get("message") or "")[:120])
    return Result(True, f"{number}: the Shiprocket booking is cancelled"
                        + (" and its rider released." if history[-1].get("awb") else ".")
                        + " Mark ready books again.")


async def check_rider(session: AsyncSession, order: Order) -> Result:
    """Ask Shiprocket where this order's rider is, and keep the answer."""
    info = details(order)
    if not info.get("shipment_id"):
        return Result(False, f"{order.order_number} has no Shiprocket shipment yet.")
    try:
        found = shiprocket.tracking(await shiprocket.track_shipment(info["shipment_id"]))
    except ShiprocketError as exc:
        return Result(False, f"Could not reach Shiprocket for {order.order_number}: {exc.message}")
    changes = {k: v for k, v in {
        "status": found["status"], "awb": found["awb"], "rider": found["rider"],
        "eta": found["eta"], "tracking_url": found["url"],
        "courier_name": found["courier"]}.items() if v}
    changes["checked_at"] = datetime.now(timezone.utc).isoformat()
    _save(order, **changes)
    await session.flush()
    status = found["status"] or found["error"] or "no update yet"
    return Result(True, f"{order.order_number}: {status}"
                        + (f" · rider {found['rider']}" if found["rider"] else ""))
