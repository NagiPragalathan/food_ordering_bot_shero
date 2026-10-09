"""Payments that go wrong at the edges (services/payments.settle_lapsed_link,
slots.book_after_late_payment).

Live money means a lost or late Stripe webhook must never cost a customer:
a paid order must not be told it expired, and a payment that lands after the
link lapsed must still get its delivery slot.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.exceptions import PaymentError
from app.db.models import DeliverySlot, Order, OrderStage, PaymentStatus, SlotHold, SlotHoldStatus
from app.services import payments, slots

pytestmark = pytest.mark.asyncio

LINES = [{"retailer_id": "SAMBAR-1", "name": "Sambar", "quantity": 2,
          "unit_price": "9.50", "line_total": "19.00"}]


@pytest.fixture
def quiet(monkeypatch):
    """No WhatsApp, Zoho, Uber or team-alert calls; records the templates sent."""
    sent = []

    class Sender:
        async def send_template(self, to, spec, *values, **kw):
            sent.append(spec.name)

    async def nothing(*_a, **_k):
        return True
    monkeypatch.setattr(payments, "current_sender", lambda: Sender())
    monkeypatch.setattr(payments.crm_sync, "advance_stage", nothing)
    monkeypatch.setattr(payments.crm_sync, "convert_and_record_order", nothing)
    monkeypatch.setattr(payments.order_alerts, "notify_new_order", nothing)
    return sent


async def _slot(session, outlet, *, capacity=2, reserved=0) -> DeliverySlot:
    start = datetime.now(timezone.utc) + timedelta(days=2)
    slot = DeliverySlot(outlet_id=outlet.id, starts_at=start, ends_at=start + timedelta(hours=1),
                        capacity=capacity, reserved_count=reserved)
    session.add(slot)
    await session.flush()
    return slot


async def _lapsed_order(session, customer, outlet, slot, *, held=True) -> Order:
    order = Order(order_number="SHO-SAFE-1", customer_id=customer.id, outlet_id=outlet.id,
                  slot_id=slot.id, items=LINES, item_count=2, dish_total=Decimal("19.00"),
                  total=Decimal("22.00"), stripe_session_id="cs_live_abc",
                  payment_status=PaymentStatus.LINK_SENT, stage=OrderStage.PENDING_PAYMENT,
                  payment_link_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    session.add(order)
    await session.flush()
    if held:
        await slots.hold_slot(session, slot_id=slot.id, order_id=order.id)
    return order


def _stripe(monkeypatch, *states, fail=False):
    """Stripe answers these session states in turn; records expire calls."""
    answers, expired = list(states), []

    async def state(session_id):
        if fail:
            raise PaymentError("Stripe is down")
        return answers.pop(0) if len(answers) > 1 else answers[0]

    async def expire(session_id):
        expired.append(session_id)
    monkeypatch.setattr(payments.checkout, "session_state", state)
    monkeypatch.setattr(payments.checkout, "expire_session", expire)
    return expired


PAID = {"status": "complete", "payment_status": "paid", "payment_intent": "pi_live_1"}
OPEN = {"status": "open", "payment_status": "unpaid", "payment_intent": None}
EXPIRED = {"status": "expired", "payment_status": "unpaid", "payment_intent": None}


# --- the expiry job asks Stripe first ----------------------------------------------
async def test_a_paid_order_whose_webhook_was_lost_is_confirmed_not_expired(
        session, customer, outlet, quiet, monkeypatch):
    slot = await _slot(session, outlet)
    order = await _lapsed_order(session, customer, outlet, slot)
    _stripe(monkeypatch, PAID)

    assert await payments.settle_lapsed_link(session, order, customer) == "paid"
    assert order.payment_status == PaymentStatus.PAID
    assert order.stripe_payment_intent_id == "pi_live_1"
    assert "payment_expired_v2" not in quiet           # never told it expired


async def test_an_open_link_is_closed_at_stripe_before_the_slot_goes(
        session, customer, outlet, quiet, monkeypatch):
    slot = await _slot(session, outlet)
    order = await _lapsed_order(session, customer, outlet, slot)
    expired = _stripe(monkeypatch, OPEN, EXPIRED)

    assert await payments.settle_lapsed_link(session, order, customer) == "expired"
    assert expired == ["cs_live_abc"]
    assert order.payment_status == PaymentStatus.EXPIRED


async def test_paid_in_the_last_second_before_closing_is_still_paid(
        session, customer, outlet, quiet, monkeypatch):
    slot = await _slot(session, outlet)
    order = await _lapsed_order(session, customer, outlet, slot)
    _stripe(monkeypatch, OPEN, PAID)                     # paid while we closed it
    assert await payments.settle_lapsed_link(session, order, customer) == "paid"


async def test_when_stripe_cannot_be_asked_nothing_is_expired(
        session, customer, outlet, quiet, monkeypatch):
    slot = await _slot(session, outlet)
    order = await _lapsed_order(session, customer, outlet, slot)
    _stripe(monkeypatch, fail=True)

    assert await payments.settle_lapsed_link(session, order, customer) == "unchecked"
    assert order.payment_status == PaymentStatus.LINK_SENT   # tried again next run
    assert quiet == []


# --- a payment after the link lapsed keeps its slot ------------------------------
async def _booked(session, order) -> list[SlotHold]:
    rows = await session.execute(select(SlotHold).where(
        SlotHold.order_id == order.id, SlotHold.status == str(SlotHoldStatus.BOOKED)))
    return list(rows.scalars())


async def test_a_late_payment_claims_its_slot_again(session, customer, outlet, quiet):
    slot = await _slot(session, outlet)
    order = await _lapsed_order(session, customer, outlet, slot)
    await payments.handle_payment_expired(session, order, customer)     # hold released
    assert slot.reserved_count == 0

    assert await payments.handle_payment_success(session, order, customer, "pi_live_2")
    assert len(await _booked(session, order)) == 1
    await session.refresh(slot)
    assert slot.reserved_count == 1


async def test_a_late_payment_into_a_full_slot_is_still_booked(
        session, customer, outlet, quiet):
    """One extra order for the kitchen beats a paid customer with no delivery."""
    slot = await _slot(session, outlet, capacity=1)
    order = await _lapsed_order(session, customer, outlet, slot, held=False)
    slot.reserved_count = 1                               # someone else took it
    await session.flush()

    assert await payments.handle_payment_success(session, order, customer, "pi_live_3")
    assert len(await _booked(session, order)) == 1
    await session.refresh(slot)
    assert slot.reserved_count == 2 and order.payment_status == PaymentStatus.PAID
