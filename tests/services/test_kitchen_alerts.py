"""The kitchen hears about a paid order, with nothing about the customer
(services/kitchen_alerts.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.db.models import DeliverySlot, Order, OrderStage, PaymentStatus
from app.integrations.gallabox import fallback
from app.integrations.gallabox import templates as tpl
from app.services import kitchen_alerts

pytestmark = pytest.mark.asyncio

# Tue 6 Oct 2026, 18:00 in New York (EDT, UTC-4) = 22:00 UTC.
SLOT_START = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc)


class Sender:
    def __init__(self, error: Exception | None = None):
        self.sent, self.error = [], error

    async def send_template(self, to, spec, *values):
        if self.error:
            raise self.error
        self.sent.append((to, spec.name, values))


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

    (to, template, values), = sender.sent
    assert to == "14438011011" and template == "shero_kitchen_order"
    assert "SHO-K-1" in values and "2 x Carrot Sambar" in values[-1]
    assert order.stage == OrderStage.SENT_TO_KITCHEN
    assert order.kitchen_notified_at == that_morning
    assert await kitchen_alerts.due_alerts(session, that_morning) == []


async def test_the_partner_gets_the_order_and_nothing_about_the_customer(
        session, customer, outlet, monkeypatch):
    """Order code, delivery, kitchen and dishes: no name, number, address or
    amount (the client: partners must not get the customer's details)."""
    outlet.kitchen_whatsapp = "14438011011"
    order = await _paid_order(session, customer, outlet)
    order.apartment_unit, order.postal_code = "Apt 4", "21075"
    order.contact_number = "14105550123"
    sender = Sender()
    monkeypatch.setattr(kitchen_alerts, "current_sender", lambda: sender)

    assert await kitchen_alerts.send_alert(session, order)
    (_, template, values), = sender.sent
    assert template == tpl.KITCHEN_ORDER.name
    assert values == ("SHO-K-1", "Tue 6 Oct, 6:00 PM", outlet.name, "2 x Carrot Sambar")
    text = fallback.body_text(tpl.KITCHEN_ORDER, values)
    for private in (customer.name, customer.whatsapp_number, order.contact_number,
                    "12 Maple St", "Apt 4", "21075", "20.00", "Customer", "Address",
                    "Amount"):
        assert private not in text


async def test_until_meta_approves_it_the_team_template_goes_with_the_customer_blanked(
        session, customer, outlet, monkeypatch):
    """The kitchen still hears outside the 24-hour window while
    shero_kitchen_order waits for approval, without the customer's details."""
    outlet.kitchen_whatsapp = "14438011011"
    order = await _paid_order(session, customer, outlet)
    sender = Sender()
    monkeypatch.setattr(kitchen_alerts, "current_sender", lambda: sender)

    async def approved(name):
        return name != tpl.KITCHEN_ORDER.name
    monkeypatch.setattr(kitchen_alerts.template_status, "is_approved", approved)

    assert await kitchen_alerts.send_alert(session, order)
    (_, template, values), = sender.sent
    assert template == tpl.ORDER_ALERT.name
    assert values == ("SHO-K-1", "-", "-", "Tue 6 Oct, 6:00 PM", outlet.name, "-",
                      "2 x Carrot Sambar")


async def test_with_neither_approved_the_partner_template_goes(
        session, customer, outlet, monkeypatch):
    """Gallabox unreachable or nothing approved: the client sends the partner
    template's words as text, never the team's."""
    async def never(_name):
        return False
    monkeypatch.setattr(kitchen_alerts.template_status, "is_approved", never)
    assert await kitchen_alerts.template() is tpl.KITCHEN_ORDER


async def test_a_kitchen_without_whatsapp_stays_pending(
        session, customer, outlet, monkeypatch):
    outlet.kitchen_whatsapp = None
    order = await _paid_order(session, customer, outlet)
    sender = Sender()
    monkeypatch.setattr(kitchen_alerts, "current_sender", lambda: sender)

    assert await kitchen_alerts.send_alert(session, order) is False
    assert sender.sent == [] and order.stage == OrderStage.PAID_SLOT_BOOKED
    assert order.kitchen_notified_at is None


async def test_a_whatsapp_failure_is_logged_not_raised(session, customer, outlet, monkeypatch):
    outlet.kitchen_whatsapp = "14438011011"
    order = await _paid_order(session, customer, outlet)
    monkeypatch.setattr(kitchen_alerts, "current_sender",
                        lambda: Sender(error=RuntimeError("gallabox down")))

    assert await kitchen_alerts.send_alert(session, order) is False
    assert order.kitchen_notified_at is None
    assert order.stage == OrderStage.PAID_SLOT_BOOKED


async def test_failed_alert_retries_and_success_is_not_sent_twice(session, customer, outlet, monkeypatch):
    outlet.kitchen_whatsapp = "14438011011"
    order = await _paid_order(session, customer, outlet)
    now = datetime.now(timezone.utc)
    order.kitchen_notify_at = now
    # Keep the delivery window in the future for the retry worker.
    slot = await session.get(DeliverySlot, order.slot_id)
    slot.ends_at = now + timedelta(days=2)
    sender = Sender(error=RuntimeError("down"))
    monkeypatch.setattr(kitchen_alerts, "current_sender", lambda: sender)
    assert not await kitchen_alerts.send_alert(session, order)
    assert order in await kitchen_alerts.due_alerts(session, now)
    sender.error = None
    assert await kitchen_alerts.send_alert(session, order)
    assert await kitchen_alerts.send_alert(session, order)
    assert len(sender.sent) == 1
    assert order not in await kitchen_alerts.due_alerts(session, now)


async def test_large_cart_sends_every_item(session, customer, outlet, monkeypatch):
    outlet.kitchen_whatsapp = "14438011011"
    order = await _paid_order(session, customer, outlet)
    order.items = [{"name": f"Dish {i} with extra vegetables", "quantity": i + 1} for i in range(30)]
    sender = Sender()
    monkeypatch.setattr(kitchen_alerts, "current_sender", lambda: sender)
    assert await kitchen_alerts.send_alert(session, order)
    combined = "".join(values[-1] for _, _, values in sender.sent)
    for item in order.items:
        assert f"{item['quantity']} x {item['name']}" in combined


async def test_payment_immediately_notifies_only_assigned_partner(session, customer, outlet, far_outlet, monkeypatch):
    from app.services import payments
    from app.integrations.gallabox.sender import use_sender
    outlet.kitchen_whatsapp = "14438011011"
    far_outlet.kitchen_whatsapp = "14438011022"
    order = await _paid_order(session, customer, outlet,
                              start=datetime.now(timezone.utc) + timedelta(days=2))
    order.payment_status = PaymentStatus.LINK_SENT
    order.stage = OrderStage.PENDING_PAYMENT
    sender = Sender()

    async def nothing(*args, **kwargs):
        return True

    monkeypatch.setattr(payments, "send_payment_confirmation", nothing)
    monkeypatch.setattr(payments.crm_sync, "convert_and_record_order", nothing)
    monkeypatch.setattr(payments.order_alerts, "notify_new_order", nothing)
    with use_sender(sender):
        assert await payments.handle_payment_success(session, order, customer)
        assert not await payments.handle_payment_success(session, order, customer)
    assert len(sender.sent) == 1
    assert sender.sent[0][0] == outlet.kitchen_whatsapp
    assert order.kitchen_notified_at is not None
    assert order.stage == OrderStage.SENT_TO_KITCHEN
