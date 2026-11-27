"""The kitchen hears about a paid order, with each dish's quantity and PPP
and nothing about the customer or the MRP (services/kitchen_alerts.py)."""

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
# The partner's dishes: quantity and PPP, never the $9.75 MRP.
CARROT = "2 x Carrot Sambar (PPP $6.78 each, $13.56); Total PPP: $13.56"


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
                  items=[{"retailer_id": "chettinad-sambar-carrot-sambar",
                          "name": "Carrot Sambar", "quantity": 2, "unit_price": "9.75",
                          "line_total": "19.50", "unit_ppp": "6.78"}],
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
    assert values == ("SHO-K-1", "Tue 6 Oct, 6:00 PM", outlet.name, CARROT)
    text = fallback.body_text(tpl.KITCHEN_ORDER, values)
    for private in (customer.name, customer.whatsapp_number, order.contact_number,
                    "12 Maple St", "Apt 4", "21075", "20.00", "Customer", "Address",
                    "Amount", "MRP", "9.75", "19.50"):
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
    assert values == ("SHO-K-1", "-", "-", "Tue 6 Oct, 6:00 PM", outlet.name, "-", CARROT)


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
    assert len(sender.sent) > 1
    parts = [values[-1] for _, _, values in sender.sent]
    assert parts[0].startswith(f"(1/{len(parts)}) ") and all(len(p) <= 300 for p in parts)
    # Every dish whole in one message, never cut between two.
    for item in order.items:
        dish = f"{item['quantity']} x {item['name']} (PPP not set)"
        assert sum(dish + ";" in p + ";" for p in parts) == 1


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


async def test_each_dish_carries_its_ppp_and_never_the_mrp(session, customer, outlet):
    """The client: "Include menu items, quantities and applicable Triple-P in
    the WhatsApp order intimation. MRP must not be displayed." A dish without
    a PPP says so, and the total says what it leaves out."""
    order = await _paid_order(session, customer, outlet)
    order.items = [
        {"retailer_id": "a", "name": "Poondu (Garlic) Rasam - 450 ml", "quantity": 2,
         "unit_price": "157.00", "line_total": "314.00", "unit_ppp": "44.00"},
        {"retailer_id": "b", "name": "Vazhaikkai Podimas - 450 ml", "quantity": 1,
         "unit_price": "210.00", "line_total": "210.00", "unit_ppp": "68.00"},
        {"retailer_id": "c", "name": "New Dish", "quantity": 1, "unit_price": "99.00"}]
    texts = await kitchen_alerts.partner_items(session, order)
    assert texts == ["2 x Poondu (Garlic) Rasam - 450 ml (PPP $44.00 each, $88.00)",
                     "1 x Vazhaikkai Podimas - 450 ml (PPP $68.00)",
                     "1 x New Dish (PPP not set)",
                     "Total PPP: $156.00, plus 1 dish without a PPP"]
    joined = " ".join(texts)
    for mrp in ("157", "314", "210", "99"):
        assert mrp not in joined


async def test_an_order_from_before_takes_the_menus_ppp(session, customer, outlet, menu):
    """Orders priced before the PPP was kept on the order: the menu's PPP."""
    order = await _paid_order(session, customer, outlet)
    order.items = [{"retailer_id": "chettinad-sambar-drumstick-sambar",
                    "name": "Drumstick Sambar", "quantity": 3, "unit_price": "12.50"}]
    assert await kitchen_alerts.partner_items(session, order) == [
        "3 x Drumstick Sambar (PPP $8.16 each, $24.48)", "Total PPP: $24.48"]


async def test_an_order_is_priced_with_the_ppp_it_keeps(session, menu):
    """pricing keeps each dish's PPP on the order line for the partner's
    message; the customer's price stays the MRP."""
    from app.services import pricing
    priced = await pricing.price_cart(
        session, [{"retailer_id": "chettinad-sambar-drumstick-sambar", "quantity": 2}])
    (line,) = priced.items_as_dicts()
    assert line["unit_price"] == "12.50" and line["unit_ppp"] == "8.16"
    assert priced.dish_total == Decimal("25.00")
