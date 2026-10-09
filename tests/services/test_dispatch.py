"""The Uber courier is booked 2 hours before the delivery slot
(services/dispatch.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.exceptions import IntegrationError
from app.db.models import DeliverySlot, Order, OrderStage, PaymentStatus
from app.integrations.uber import direct as uber
from app.services import dispatch

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def uber_calls(monkeypatch):
    calls: list[dict] = []

    async def create_delivery(**kw):
        calls.append(kw)
        return uber.Delivery(delivery_id="del_1", status="pending",
                             tracking_url="https://track/del_1")
    monkeypatch.setattr(dispatch.uber, "create_delivery", create_delivery)
    return calls


async def _order(session, customer, outlet, *, starts_in: timedelta, number="SHO-D-1",
                 paid=True) -> Order:
    outlet.phone = "17325550100"
    start = NOW + starts_in
    slot = DeliverySlot(outlet_id=outlet.id, starts_at=start,
                        ends_at=start + timedelta(hours=1), capacity=2)
    session.add(slot)
    await session.flush()
    order = Order(order_number=number, customer_id=customer.id, outlet_id=outlet.id,
                  slot_id=slot.id, total=Decimal("20"),
                  items=[{"retailer_id": "a", "name": "Carrot Sambar", "quantity": 2,
                          "unit_price": "10"}],
                  delivery_address="12 Maple St", apartment_unit="2B", postal_code="08820",
                  delivery_latitude=40.52, delivery_longitude=-74.41,
                  contact_number="17325550142", delivery_instructions="Ring the bell",
                  payment_status=PaymentStatus.PAID if paid else PaymentStatus.LINK_SENT,
                  stage=OrderStage.SENT_TO_KITCHEN)
    session.add(order)
    await session.flush()
    return order


async def test_an_order_is_due_two_hours_before_its_slot(session, customer, outlet):
    later = await _order(session, customer, outlet, starts_in=timedelta(hours=3), number="L")
    soon = await _order(session, customer, outlet, starts_in=timedelta(hours=1, minutes=59),
                        number="S")
    await _order(session, customer, outlet, starts_in=timedelta(hours=1), number="U",
                 paid=False)

    due = await dispatch.due_orders(session, NOW)

    assert [o.order_number for o in due] == ["S"]
    assert later not in due and soon in due


async def test_marking_out_for_delivery_by_hand_does_not_stop_the_booking(
        session, customer, outlet):
    """SHO-261007-HANF9 was moved to Out for Delivery on the Orders page before
    its send time, so the job skipped it and the slot ended with no courier."""
    order = await _order(session, customer, outlet, starts_in=timedelta(hours=1))
    order.stage = OrderStage.OUT_FOR_DELIVERY
    assert order in await dispatch.due_orders(session, NOW)

    for finished in (OrderStage.DELIVERED, OrderStage.CANCELLED, OrderStage.REFUNDED):
        order.stage = finished
        assert await dispatch.due_orders(session, NOW) == []


async def test_dispatch_books_the_courier_inside_the_slot(session, customer, outlet, uber_calls):
    order = await _order(session, customer, outlet, starts_in=timedelta(hours=2))

    assert await dispatch.dispatch(session, order, now=NOW)

    (call,) = uber_calls
    assert call["pickup_name"] == "Shero Edison" and call["pickup_phone"] == "+17325550100"
    assert call["dropoff_phone"] == "+17325550142"
    assert call["dropoff"]["street_address"] == ["12 Maple St", "2B"]
    assert call["items"] == [{"name": "Carrot Sambar", "quantity": 2, "size": "small"}]
    assert call["external_id"] == "SHO-D-1"
    assert call["pickup_ready_at"] == NOW
    assert call["dropoff_ready_at"] == NOW + timedelta(hours=2)
    assert call["dropoff_deadline_at"] == NOW + timedelta(hours=3)
    assert call["dropoff_notes"] == "Ring the bell"
    assert order.uber_delivery_id == "del_1" and order.uber_tracking_url == "https://track/del_1"
    assert order.uber_dispatch_error is None

    # Booked once: it is no longer due.
    assert await dispatch.due_orders(session, NOW) == []


async def test_a_failure_is_kept_and_retried_next_run(session, customer, outlet, monkeypatch):
    order = await _order(session, customer, outlet, starts_in=timedelta(hours=1))

    async def down(**_kw):
        raise IntegrationError("uber", "503 from Uber")
    monkeypatch.setattr(dispatch.uber, "create_delivery", down)

    assert await dispatch.dispatch(session, order, now=NOW) is False
    assert order.uber_dispatch_error and "503" in order.uber_dispatch_error
    assert order in await dispatch.due_orders(session, NOW)       # tried again


async def test_a_kitchen_without_a_phone_is_reported_not_sent(
        session, customer, outlet, uber_calls):
    order = await _order(session, customer, outlet, starts_in=timedelta(hours=1))
    outlet.phone = outlet.kitchen_whatsapp = None

    assert await dispatch.dispatch(session, order, now=NOW) is False
    assert uber_calls == [] and order.uber_dispatch_error
    assert "phone" in order.uber_dispatch_error


# --- the queue ------------------------------------------------------------------
async def test_the_stored_send_time_decides_when_it_goes(session, customer, outlet):
    order = await _order(session, customer, outlet, starts_in=timedelta(hours=5))
    order.uber_dispatch_due_at = NOW - timedelta(minutes=1)   # e.g. sent early by a setting
    assert order in await dispatch.due_orders(session, NOW)

    order.uber_dispatch_due_at = NOW + timedelta(hours=3)
    assert await dispatch.due_orders(session, NOW) == []


def test_the_send_time_is_two_hours_before_the_slot():
    assert dispatch.due_time(NOW) == NOW - timedelta(hours=2)
    assert dispatch.due_time(None) is None


async def test_the_queue_shows_each_orders_state(session, customer, outlet, uber_calls):
    waiting = await _order(session, customer, outlet, starts_in=timedelta(hours=26), number="W")
    booked = await _order(session, customer, outlet, starts_in=timedelta(hours=1), number="B")
    await dispatch.dispatch(session, booked, now=NOW)
    missed = await _order(session, customer, outlet, starts_in=timedelta(hours=-3), number="M")

    rows = {r.order.order_number: r for r in await dispatch.queue(session, NOW)}

    assert rows["W"].status == dispatch.WAITING
    assert rows["W"].send_at == NOW + timedelta(hours=24)
    assert rows["B"].status == dispatch.BOOKED
    assert rows["M"].status == dispatch.MISSED
    assert waiting and missed


async def test_a_booking_can_be_cancelled(session, customer, outlet, uber_calls, monkeypatch):
    order = await _order(session, customer, outlet, starts_in=timedelta(hours=1))
    await dispatch.dispatch(session, order, now=NOW)

    async def cancel_delivery(delivery_id):
        assert delivery_id == "del_1"
        return "canceled"
    monkeypatch.setattr(dispatch.uber, "cancel_delivery", cancel_delivery)

    assert await dispatch.cancel(order)
    (row,) = await dispatch.queue(session, NOW)
    assert row.status == dispatch.CANCELLED


# --- the send time setting and the test-mode pause --------------------------------
async def test_changing_the_hours_moves_orders_already_waiting(session, customer, outlet,
                                                                monkeypatch):
    from app.core.config import settings

    order = await _order(session, customer, outlet, starts_in=timedelta(days=1))
    slot_start = NOW + timedelta(days=1)
    order.uber_dispatch_due_at = slot_start - timedelta(hours=2)

    monkeypatch.setattr(settings, "uber_dispatch_hours_before", 3.0)
    assert await dispatch.reschedule(session, NOW) == 1
    assert dispatch.as_utc(order.uber_dispatch_due_at) == slot_start - timedelta(hours=3)
    # The kitchen still hears no later than the courier is booked.
    assert dispatch.as_utc(order.kitchen_notify_at) <= slot_start - timedelta(hours=3)


async def test_a_booked_order_keeps_its_time(session, customer, outlet, monkeypatch):
    from app.core.config import settings

    order = await _order(session, customer, outlet, starts_in=timedelta(days=1))
    order.uber_delivery_id = "del_9"
    monkeypatch.setattr(settings, "uber_dispatch_hours_before", 3.0)
    assert await dispatch.reschedule(session, NOW) == 0


def test_booking_pauses_while_stripe_is_in_test_mode(monkeypatch):
    """With Uber live, a fake-card payment would book a real, paid courier."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_123")
    assert "test mode" in dispatch.auto_booking_paused()
    monkeypatch.setattr(settings, "stripe_secret_key", "rk_live_123")
    assert dispatch.auto_booking_paused() == ""


async def test_the_job_books_nothing_while_paused(monkeypatch):
    from app.core.config import settings
    from app.workers import jobs

    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_123")

    async def never(*_a, **_k):
        raise AssertionError("the queue must not even be read")
    monkeypatch.setattr(jobs.dispatch, "due_orders", never)
    assert await jobs.dispatch_couriers() == 0
