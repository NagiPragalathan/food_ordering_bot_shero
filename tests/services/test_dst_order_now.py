"""DST India takes orders for now, as any food delivery app does: "Now" (the
delivery window under way when the food would arrive, about 45 minutes after
ordering) and every later window, today or later in the week. Shero US keeps
its day's notice (app/services/slots.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core import tenants
from app.core.config import settings_for
from app.db.models import DeliverySlot
from app.services import settings_store, slots

NOW_SLACK = timedelta(seconds=5)


@pytest.fixture
async def kitchen(outlet):
    """An Indian kitchen open round the clock, so "Now" always has a window."""
    outlet.timezone = "Asia/Kolkata"
    outlet.operating_hours = {day: [["00:00", "00:00"]]
                              for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    return outlet


def test_dst_india_takes_orders_for_now_and_shero_us_keeps_a_days_notice():
    dst, us = settings_for(tenants.DST), settings_for(tenants.US)
    assert dst.instant_orders is True and dst.instant_lead_minutes == 45
    assert us.instant_orders is False and us.slot_min_lead_hours == 24
    with tenants.use(tenants.DST):
        assert slots.lead_time() == timedelta(minutes=45)
    assert slots.lead_time() == timedelta(hours=24)


async def test_now_is_the_window_the_food_would_arrive_in(session, kitchen):
    with tenants.use(tenants.DST):
        options = await slots.list_available_slots(session, kitchen, days_ahead=2, limit=50)
    assert options
    first, later = options[0], options[1:]
    arrives = datetime.now(timezone.utc) + timedelta(minutes=45)
    # "Now": under way when the food would arrive, labelled with its day.
    assert first.now
    assert first.starts_at <= arrives + NOW_SLACK and first.ends_at > arrives - NOW_SLACK
    assert first.label.endswith(", next 45 min")
    # Then every later window, the same day included: no day's notice.
    assert later and not any(o.now for o in later)
    assert all(o.starts_at >= first.ends_at for o in later)
    assert later[0].starts_at < datetime.now(timezone.utc) + timedelta(hours=3)


async def test_shero_us_still_offers_nothing_inside_24_hours(session, kitchen):
    options = await slots.list_available_slots(session, kitchen, days_ahead=2, limit=50)
    assert options
    soonest = datetime.now(timezone.utc) + timedelta(hours=24) - NOW_SLACK
    assert all(o.starts_at >= soonest and not o.now for o in options)


async def test_the_order_now_time_is_a_setting(session, kitchen, monkeypatch):
    monkeypatch.setattr(settings_for(tenants.DST), "instant_lead_minutes", 90)
    with tenants.use(tenants.DST):
        options = await slots.list_available_slots(session, kitchen, days_ahead=1)
        title = slots.now_title()
    arrives = datetime.now(timezone.utc) + timedelta(minutes=90)
    assert options[0].now and options[0].ends_at > arrives - NOW_SLACK
    assert options[0].starts_at <= arrives + NOW_SLACK
    assert options[0].label.endswith(", next 90 min")
    assert title == "Now (about 90 min)" and len(title) <= 24     # a WhatsApp list row


async def test_a_full_now_window_offers_the_next_time_instead(session, kitchen):
    with tenants.use(tenants.DST):
        window = await slots.now_slot(session, kitchen)
        assert window is not None
        window.reserved_count = window.capacity
        await session.flush()
        assert await slots.now_slot(session, kitchen) is None
        options = await slots.list_available_slots(session, kitchen, days_ahead=1)
    assert options and not options[0].now
    assert options[0].starts_at >= slots.as_utc(window.ends_at)


async def test_a_window_is_bookable_until_it_ends_for_dst_india_only(session, kitchen):
    now = datetime.now(timezone.utc)
    under_way = (now - timedelta(minutes=10), now + timedelta(minutes=50))
    too_soon = (now - timedelta(minutes=30), now + timedelta(minutes=30))
    with tenants.use(tenants.DST):
        assert slots.is_bookable(*under_way, now) and slots.is_now(*under_way, now)
        assert not slots.is_bookable(*too_soon, now)     # over before the food arrives
    # Shero US: a window has to start a day away.
    assert not slots.is_bookable(*under_way, now) and not slots.is_now(*under_way, now)
    assert slots.is_bookable(now + timedelta(hours=25), now + timedelta(hours=26), now)


async def test_now_on_the_page_is_looked_up_afresh(session, kitchen):
    """A "Now" chosen a few minutes ago means now: the server finds the
    window again instead of trusting the one the page was shown."""
    from app.api.routes import order_web

    with tenants.use(tenants.DST):
        current = await slots.now_slot(session, kitchen)
        stale = (await session.execute(select(DeliverySlot).where(
            DeliverySlot.outlet_id == kitchen.id,
            DeliverySlot.ends_at <= datetime.now(timezone.utc)))).scalars().first()
        body = {"slot_id": str(stale.id) if stale else "", "now": True}
        chosen = await order_web._kitchen_slot(session, kitchen, body["slot_id"], now=True)
        assert chosen is not None and chosen.id == current.id
        assert order_web._slot_label(chosen, kitchen, body).endswith(", next 45 min")
        # A later time keeps its own window and label.
        later = {"slot_id": "x", "now": False}
        assert " - " in order_web._slot_label(chosen, kitchen, later)
    # Shero US ignores "now": a window under way is never bookable there.
    assert await order_web._kitchen_slot(session, kitchen, str(current.id), now=True) is None
    assert order_web._wants_now({"now": True}) is False


def test_the_order_now_minutes_are_on_dst_indias_settings_page_only():
    def keys():
        return {key for fields in settings_store.setting_groups().values() for key, _, _ in fields}

    assert "INSTANT_LEAD_MINUTES" not in keys()
    with tenants.use(tenants.DST):
        assert "INSTANT_LEAD_MINUTES" in keys()
    settings_store.check_value("INSTANT_LEAD_MINUTES", "30")
    for bad in ("5", "300", "45.5", "soon"):
        with pytest.raises(settings_store.SettingValueError):
            settings_store.check_value("INSTANT_LEAD_MINUTES", bad)
