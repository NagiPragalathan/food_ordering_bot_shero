"""The kitchen hears about a paid order on its delivery day
(services/kitchen_alerts.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.db.models import DeliverySlot, Order, OrderStage, PaymentStatus
from app.services import kitchen_alerts

pytestmark = pytest.mark.asyncio

# Tue 6 Oct 2026, 18:00 in New York (EDT, UTC-4) = 22:00 UTC.
SLOT_START = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc)


class Sender:
    def __init__(self, error: Exception | None = None):
        self.sent, self.error = [], error

    async def send_text(self, to, body):
        if self.error:
            raise self.error
        self.sent.append((to, body))


@pytest.fixture(autouse=True)
def _no_zoho(monkeypatch):
    async def push(*_a, **_kw):
        return None
    monkeypatch.setattr(kitchen_alerts.crm_sync, "push_order_stage", push)


async def _paid_order(session, customer, outlet, start=SLOT_START) -> Order:
    slot = DeliverySlot(outlet_id=outlet.id, starts_at=start,
                        ends_at=start + timedelta(hours=1), capacity=2)
    session.add(slot)
    await session.flush()
    order = Order(order_number="SHO-K-1", customer_id=customer.id, outlet_id=outlet.id,
                  slot_id=slot.id, total=Decimal("20"),
                  items=[{"name": "Carrot Sambar", "quantity": 2}],
                  delivery_address="12 Maple St", slot_label="Tue 6 Oct, 6:00 PM",
                  payment_status=PaymentStatus.PAID, stage=OrderStage.PAID_SLOT_BOOKED)
    order.kitchen_notify_at = kitchen_alerts.notify_time(start, outlet)
    session.add(order)
    await session.flush()
    return order


def test_the_alert_is_the_morning_of_the_delivery_day(outlet):
    # 07:00 New York on Tue 6 Oct = 11:00 UTC.
    assert kitchen_alerts.notify_time(SLOT_START, outlet) == \
        datetime(2026, 10, 6, 11, 0, tzinfo=timezone.utc)


def test_an_early_slot_is_alerted_no_later_than_the_courier_booking(outlet):
    # 08:00 New York slot: Uber is booked at 06:00, so the kitchen hears then.
    early = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    assert kitchen_alerts.notify_time(early, outlet) == early - timedelta(hours=2)


async def test_it_waits_until_the_delivery_day_then_goes_once(
        session, customer, outlet, monkeypatch):
    outlet.kitchen_whatsapp = "14438011011"
    order = await _paid_order(session, customer, outlet)
    sender = Sender()
    monkeypatch.setattr(kitchen_alerts, "current_sender", lambda: sender)

    the_day_before = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)
    assert await kitchen_alerts.due_alerts(session, the_day_before) == []

    that_morning = datetime(2026, 10, 6, 11, 5, tzinfo=timezone.utc)
    (due,) = await kitchen_alerts.due_alerts(session, that_morning)
    assert await kitchen_alerts.send_alert(session, due, now=that_morning)

    (to, body), = sender.sent
    assert to == "14438011011" and "SHO-K-1" in body and "2 x Carrot Sambar" in body
    assert order.stage == OrderStage.SENT_TO_KITCHEN
    assert order.kitchen_notified_at == that_morning
    assert await kitchen_alerts.due_alerts(session, that_morning) == []


async def test_a_kitchen_without_whatsapp_still_moves_the_order_on(
        session, customer, outlet, monkeypatch):
    outlet.kitchen_whatsapp = None
    order = await _paid_order(session, customer, outlet)
    sender = Sender()
    monkeypatch.setattr(kitchen_alerts, "current_sender", lambda: sender)

    assert await kitchen_alerts.send_alert(session, order) is False
    assert sender.sent == [] and order.stage == OrderStage.SENT_TO_KITCHEN


async def test_a_whatsapp_failure_is_logged_not_raised(session, customer, outlet, monkeypatch):
    outlet.kitchen_whatsapp = "14438011011"
    order = await _paid_order(session, customer, outlet)
    monkeypatch.setattr(kitchen_alerts, "current_sender",
                        lambda: Sender(error=RuntimeError("gallabox down")))

    assert await kitchen_alerts.send_alert(session, order) is False
    assert order.kitchen_notified_at is not None
