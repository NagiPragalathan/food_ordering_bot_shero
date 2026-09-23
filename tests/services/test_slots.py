"""Slot generation, capacity and holds."""

import itertools
from datetime import datetime, timedelta, timezone

import pytest

from app.core.exceptions import NoSlotsAvailableError
from app.db.models import Customer, Order, SlotHold, SlotHoldStatus
from app.services.slots import (
    _windows_for_day,
    book_slot,
    ensure_slots,
    hold_slot,
    list_available_slots,
    release_holds_for_order,
)


def test_windows_split_operating_hours_into_fixed_length_slots(outlet):
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/New_York")
    day = datetime(2026, 9, 21, tzinfo=tz).date()  # a Monday
    windows = _windows_for_day(outlet, day, tz)

    # 11:00-21:00 in 60-minute windows = 10 slots.
    assert len(windows) == 10
    assert windows[0][0].hour == 11
    assert windows[-1][1].hour == 21


def test_windows_are_empty_on_a_closed_day(outlet):
    from zoneinfo import ZoneInfo

    outlet.operating_hours = {"mon": [["11:00", "21:00"]]}
    tz = ZoneInfo("America/New_York")
    tuesday = datetime(2026, 9, 22, tzinfo=tz).date()
    assert _windows_for_day(outlet, tuesday, tz) == []


def test_malformed_operating_hours_are_skipped_not_fatal(outlet):
    """A bad row in the config must not take the whole day's slots down."""
    from zoneinfo import ZoneInfo

    outlet.operating_hours = {
        "mon": [["not-a-time", "21:00"], ["11:00", "13:00"]],
    }
    tz = ZoneInfo("America/New_York")
    monday = datetime(2026, 9, 21, tzinfo=tz).date()
    windows = _windows_for_day(outlet, monday, tz)
    assert len(windows) == 2  # only the valid 11:00-13:00 range survives


def test_window_crossing_midnight_is_handled(outlet):
    from zoneinfo import ZoneInfo

    outlet.operating_hours = {"mon": [["22:00", "02:00"]]}
    tz = ZoneInfo("America/New_York")
    monday = datetime(2026, 9, 21, tzinfo=tz).date()
    windows = _windows_for_day(outlet, monday, tz)
    assert len(windows) == 4  # 22-23, 23-00, 00-01, 01-02


async def test_ensure_slots_is_idempotent(session, outlet):
    first = await ensure_slots(session, outlet, days_ahead=1)
    second = await ensure_slots(session, outlet, days_ahead=1)
    assert first > 0
    assert second == 0, "re-running must not duplicate slots"


async def test_listed_slots_respect_the_lead_time(session, outlet):
    slots = await list_available_slots(session, outlet, days_ahead=2)
    earliest_allowed = datetime.now(timezone.utc) + timedelta(minutes=59)
    assert slots, "expected some bookable slots"
    assert all(s.starts_at >= earliest_allowed for s in slots)


_counter = itertools.count(1)


async def _make_order(session, outlet) -> Order:
    """A distinct customer + draft order per call."""
    n = next(_counter)
    customer = Customer(whatsapp_number=f"1732555{n:04d}")
    session.add(customer)
    await session.flush()
    order = Order(order_number=f"SHO-260922-{n:05d}",
                  customer_id=customer.id, outlet_id=outlet.id)
    session.add(order)
    await session.flush()
    return order


async def test_hold_then_book_reserves_capacity(session, outlet):
    slots = await list_available_slots(session, outlet)
    order = await _make_order(session, outlet)

    hold = await hold_slot(session, slot_id=slots[0].slot_id, order_id=order.id)
    assert hold.status == SlotHoldStatus.HELD

    assert await book_slot(session, order.id) is True
    await session.refresh(hold)
    assert hold.status == SlotHoldStatus.BOOKED


async def test_capacity_is_exhausted_then_refused(session, outlet):
    """Outlet fixture has slot_capacity=2, so the third hold must fail."""
    slots = await list_available_slots(session, outlet)
    target = slots[0].slot_id

    for _ in range(2):
        order = await _make_order(session, outlet)
        await hold_slot(session, slot_id=target, order_id=order.id)

    third = await _make_order(session, outlet)
    with pytest.raises(NoSlotsAvailableError):
        await hold_slot(session, slot_id=target, order_id=third.id)


async def test_full_slot_disappears_from_the_offered_list(session, outlet):
    slots = await list_available_slots(session, outlet)
    target = slots[0].slot_id

    for _ in range(2):
        order = await _make_order(session, outlet)
        await hold_slot(session, slot_id=target, order_id=order.id)

    remaining = await list_available_slots(session, outlet)
    assert target not in {s.slot_id for s in remaining}


async def test_releasing_a_hold_returns_the_capacity(session, outlet):
    slots = await list_available_slots(session, outlet)
    target = slots[0].slot_id
    order = await _make_order(session, outlet)

    await hold_slot(session, slot_id=target, order_id=order.id)
    released = await release_holds_for_order(session, order.id)
    assert released == 1

    available_again = await list_available_slots(session, outlet)
    assert target in {s.slot_id for s in available_again}


async def test_switching_slots_does_not_leak_the_old_reservation(session, outlet):
    slots = await list_available_slots(session, outlet)
    order = await _make_order(session, outlet)

    await hold_slot(session, slot_id=slots[0].slot_id, order_id=order.id)
    await hold_slot(session, slot_id=slots[1].slot_id, order_id=order.id)

    from sqlalchemy import select

    result = await session.execute(
        select(SlotHold).where(SlotHold.order_id == order.id,
                               SlotHold.status == SlotHoldStatus.HELD)
    )
    active = list(result.scalars())
    assert len(active) == 1
    assert str(active[0].slot_id) == slots[1].slot_id
