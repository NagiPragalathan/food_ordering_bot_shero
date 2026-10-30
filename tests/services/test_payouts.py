"""How an order's money is split between its kitchen partner and Shero
(services/payouts.py): one percentage for every kitchen, or each kitchen's own."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.db.models import AppSetting, Order, OrderStage, PaymentStatus
from app.services import payouts

D = Decimal
_numbers = iter(range(1, 10_000))


async def paid(session, customer, outlet, food="100.00", *, delivery="8.00", coupon="0",
               stage=OrderStage.PAID_SLOT_BOOKED, paid_at=None, **extra) -> Order:
    order = Order(order_number=f"SHO-P-{next(_numbers)}", customer_id=customer.id,
                  outlet_id=outlet.id,
                  items=[{"retailer_id": "sambar", "name": "Sambar", "quantity": 1,
                          "unit_price": food, "line_total": food}],
                  dish_total=D(food), delivery_fee=D(delivery), discount=D(coupon),
                  total=D(food) + D(delivery) - D(coupon), payment_status=PaymentStatus.PAID,
                  stage=stage, paid_at=paid_at or datetime.now(timezone.utc), **extra)
    session.add(order)
    await session.flush()
    return order


async def one(session, order) -> payouts.Split:
    return (await payouts.splits(session, [order]))[0]


async def test_the_kitchen_gets_its_percentage_of_the_food(session, customer, outlet):
    order = await paid(session, customer, outlet)
    s = await one(session, order)
    # $100 of food and $8 delivery, kitchen 65%: kitchen 65, Shero 35, delivery passed on.
    assert (s.paid, s.food, s.kitchen, s.passed_on, s.shero) == (
        D("108.00"), D("100.00"), D("65.00"), D("8.00"), D("35.00"))
    assert s.paid == s.kitchen + s.passed_on + s.shero
    assert (s.percent, s.shero_percent) == (D("65"), D("35"))


async def test_changing_the_percentage_changes_every_order(session, customer, outlet):
    order = await paid(session, customer, outlet)
    saved = await payouts.save(session, {"kitchen_percent": "70"}, "ops@test")
    assert (saved.kitchen_percent, saved.shero_percent) == (D("70.00"), D("30.00"))
    s = await one(session, order)
    assert (s.kitchen, s.shero) == (D("70.00"), D("30.00"))


async def test_each_kitchen_can_have_its_own_split(session, customer, outlet, far_outlet):
    here = await paid(session, customer, outlet)
    there = await paid(session, customer, far_outlet)
    await payouts.save(session, {"same_for_all": False, "kitchen_percent": "65",
                                 "kitchens": {str(far_outlet.id): "80"}}, "ops@test")
    a, b = await payouts.splits(session, [here, there])
    assert (a.kitchen, b.kitchen) == (D("65.00"), D("80.00"))     # no own split: the shared one

    # Back to the same split for all: the kitchen's own percentage is kept but not used.
    await payouts.save(session, {"same_for_all": True, "kitchen_percent": "65",
                                 "kitchens": {str(far_outlet.id): "80"}}, "ops@test")
    assert (await one(session, there)).kitchen == D("65.00")


async def test_a_coupon_comes_out_of_sheros_part(session, customer, outlet):
    s = await one(session, await paid(session, customer, outlet, coupon="10.00"))
    assert (s.paid, s.kitchen, s.shero, s.discounts) == (D("98.00"), D("65.00"), D("25.00"),
                                                         D("10.00"))


async def test_a_cancelled_or_refunded_order_gives_the_kitchen_nothing(session, customer, outlet):
    cancelled = await one(session, await paid(session, customer, outlet,
                                              stage=OrderStage.CANCELLED))
    assert (cancelled.kitchen, cancelled.cancelled) == (D("0.00"), True)
    refunded = await paid(session, customer, outlet)
    refunded.payment_status, refunded.refund_amount = PaymentStatus.REFUNDED, D("108.00")
    s = await one(session, refunded)
    assert (s.kitchen, s.passed_on, s.shero) == (D("0.00"), D("0.00"), D("0.00"))


async def test_a_bad_percentage_is_refused(session):
    with pytest.raises(payouts.PayoutError):
        await payouts.save(session, {"kitchen_percent": "120"}, "ops@test")
    with pytest.raises(payouts.PayoutError, match="between 0 and 100"):
        await payouts.save(session, {"same_for_all": False,
                                     "kitchens": {"00000000-0000-0000-0000-000000000001": "-5"}},
                           "ops@test")


async def test_the_first_versions_saved_rules_still_load(session, outlet):
    """The first version saved a list of kitchen rules and other options."""
    session.add(AppSetting(key=payouts.KEY, is_secret=False, value=json.dumps({
        "basis": "percent", "kitchen_percent": "60", "discounts": "company",
        "payable_after": "delivered",
        "kitchens": [{"outlet_id": str(outlet.id), "basis": None, "percent": "75"}]})))
    await session.flush()
    loaded = await payouts.settings(session)
    assert (loaded.same_for_all, loaded.kitchen_percent) == (False, D("60.00"))
    assert loaded.percent_for(outlet.id) == D("75.00")


async def test_orders_by_date_and_kitchen_and_the_totals(session, customer, outlet, far_outlet):
    now = datetime.now(timezone.utc)
    recent = await paid(session, customer, outlet, paid_at=now - timedelta(days=1))
    await paid(session, customer, outlet, paid_at=now - timedelta(days=40))
    other = await paid(session, customer, far_outlet, paid_at=now - timedelta(hours=2))

    found = await payouts.orders_between(session, now - timedelta(days=7), None)
    assert [o.id for o in found] == [other.id, recent.id]                    # newest first
    only_here = await payouts.orders_between(session, now - timedelta(days=7), None, outlet.id)
    assert [o.id for o in only_here] == [recent.id]

    items = await payouts.splits(session, found)
    t = payouts.totals(items)
    assert (t.orders, t.paid, t.kitchen, t.shero, t.passed_on) == (
        2, D("216.00"), D("130.00"), D("70.00"), D("16.00"))
    assert set(payouts.by_kitchen(items)) == {outlet.id, far_outlet.id}

    text = payouts.orders_csv(items, {outlet.id: "Shero Edison"})
    assert "Kitchen gets" in text and recent.order_number in text and "Shero Edison" in text
    assert text.strip().splitlines()[-1].startswith("Total,")
