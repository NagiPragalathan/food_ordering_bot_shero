"""A kitchen's timezone follows its pin, and editing a kitchen rebuilds the
slots it offers (services/kitchen_admin.timezone_for, slots.rebuild_future).

A Chennai kitchen left on New York time offered "9 AM" slots that were
6:30 PM in Chennai, inside the 24-hour notice as the customer sees it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import DeliverySlot
from app.services import kitchen_admin, slots


def _form(**changes) -> kitchen_admin.KitchenForm:
    base = dict(name="Shero Edison", address_line1="123 Oak Tree Road", city="Edison",
                state="NJ", postal_code="08820", latitude=40.5187, longitude=-74.4121,
                service_area_mode="radius", radius_miles=10, service_zips=[],
                kitchen_whatsapp=None, phone="14155550100", timezone="America/New_York",
                operating_hours={d: [["09:00", "21:00"]] for d in slots.WEEKDAY_KEYS},
                slot_length_minutes=60, slot_capacity=4, is_active=True)
    base.update(changes)
    return kitchen_admin.KitchenForm(**base)


def test_the_timezone_comes_from_the_pin():
    assert kitchen_admin.timezone_for(13.0647, 80.2302) == "Asia/Kolkata"        # Chennai
    assert kitchen_admin.timezone_for(39.2126, -76.7233) == "America/New_York"   # Elkridge
    assert kitchen_admin.timezone_for(34.05, -118.24) == "America/Los_Angeles"
    assert kitchen_admin.timezone_for(0.0, -30.0) is None                        # at sea


async def test_saving_uses_the_pins_timezone_not_the_forms(session, outlet):
    saved = await kitchen_admin.save(session, outlet, _form(
        latitude=13.0647, longitude=80.2302, timezone="America/New_York"))
    assert saved.timezone == "Asia/Kolkata"


async def _future_starts(session, outlet) -> list[datetime]:
    rows = await session.execute(select(DeliverySlot.starts_at).where(
        DeliverySlot.outlet_id == outlet.id,
        DeliverySlot.starts_at > datetime.now(timezone.utc)))
    return [slots.as_utc(s) for s in rows.scalars()]


async def test_a_timezone_change_replaces_the_free_slots(session, outlet):
    await slots.ensure_slots(session, outlet, days_ahead=3)
    new_york_starts = set(await _future_starts(session, outlet))

    outlet.timezone = "Asia/Kolkata"
    removed = await slots.rebuild_future(session, outlet)
    await slots.ensure_slots(session, outlet, days_ahead=3)

    assert removed > 0
    starts = await _future_starts(session, outlet)
    assert not (set(starts) & new_york_starts)          # nothing left on New York hours
    # Every slot now opens inside 11:00-21:00 Chennai time.
    for start in starts:
        local = start.astimezone(slots.outlet_tz(outlet))
        assert 11 <= local.hour < 21


async def test_shorter_hours_drop_the_slots_outside_them(session, outlet):
    await slots.ensure_slots(session, outlet, days_ahead=2)
    outlet.operating_hours = {d: [["17:00", "21:00"]] for d in slots.WEEKDAY_KEYS}
    await slots.rebuild_future(session, outlet)

    for start in await _future_starts(session, outlet):
        assert start.astimezone(slots.outlet_tz(outlet)).hour >= 17


async def test_a_booked_slot_is_never_removed(session, outlet):
    await slots.ensure_slots(session, outlet, days_ahead=2)
    booked = (await session.execute(select(DeliverySlot).where(
        DeliverySlot.outlet_id == outlet.id,
        DeliverySlot.starts_at > datetime.now(timezone.utc) + timedelta(hours=30))
        .order_by(DeliverySlot.starts_at).limit(1))).scalar_one()
    booked.reserved_count = 1

    outlet.operating_hours = {}                         # closed every day now
    await slots.rebuild_future(session, outlet)

    assert slots.as_utc(booked.starts_at) in await _future_starts(session, outlet)
