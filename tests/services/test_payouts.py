"""How an order's money is split between its kitchen partner and Shero
(services/payouts.py)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.db.models import (DiscountType, Order, OrderStage, PaymentStatus, Promotion)
from app.services import payouts

D = Decimal
DRUMSTICK = "chettinad-sambar-drumstick-sambar"   # $12.50, PPP $8.16
RASAM = "chettinad-rasam-tomato-rasam"            # $9.50, PPP $6.20
_numbers = iter(range(1, 10_000))


def line(retailer_id, quantity, unit):
    return {"retailer_id": retailer_id, "name": retailer_id.split("-")[-2].title(),
            "quantity": quantity, "unit_price": str(unit), "line_total": str(D(unit) * quantity)}


async def paid(session, customer, outlet, items, *, delivery="8.00", coupon="0", points="0",
               promotion=None, stage=OrderStage.PAID_SLOT_BOOKED, paid_at=None) -> Order:
    food = sum((D(i["line_total"]) for i in items), D(0))
    discount = D(coupon) + D(points)
    order = Order(order_number=f"SHO-P-{next(_numbers)}", customer_id=customer.id,
                  outlet_id=outlet.id, items=items, dish_total=food, delivery_fee=D(delivery),
                  discount=discount, loyalty_discount=D(points),
                  promotion_id=promotion.id if promotion else None,
                  promo_code=promotion.code if promotion else None,
                  total=food + D(delivery) - discount, payment_status=PaymentStatus.PAID,
                  stage=stage, paid_at=paid_at or datetime.now(timezone.utc))
    session.add(order)
    await session.flush()
    return order


async def one(session, order) -> payouts.Split:
    return (await payouts.splits(session, [order]))[0]


async def save(session, **changes):
    data = payouts.Rules().model_dump(mode="json")
    data.update(changes)
    return await payouts.save_rules(session, data, "ops@test")


async def promo(session, kind, value="10") -> Promotion:
    p = Promotion(code=f"P{kind.upper()[:4]}", title=kind, discount_type=kind, discount_value=D(value))
    session.add(p)
    await session.flush()
    return p


# --- The split --------------------------------------------------------------------
async def test_the_default_split_is_a_percentage_of_the_food(session, customer, outlet, menu):
    order = await paid(session, customer, outlet, [line(DRUMSTICK, 8, "12.50")])
    s = await one(session, order)
    # $100 of food and $8 delivery: the kitchen gets 65, delivery is passed on.
    assert (s.paid, s.food, s.kitchen, s.passed_on, s.company) == (D("108.00"), D("100.00"),
                                                                   D("65.00"), D("8.00"), D("35.00"))
    assert s.paid == s.kitchen + s.passed_on + s.company
    assert s.rule == "65% of the food" and not s.locked and s.status == "upcoming"

    order.stage = OrderStage.DELIVERED
    assert (await one(session, order)).status == "due"
    await save(session, payable_after="paid")
    order.stage = OrderStage.PAID_SLOT_BOOKED
    assert (await one(session, order)).status == "due"


async def test_partner_prices_give_each_dish_its_own_amount(session, customer, outlet, menu):
    await save(session, basis="dish_price", kitchen_percent="60")
    order = await paid(session, customer, outlet, [
        line(DRUMSTICK, 2, "12.50"), line(RASAM, 1, "9.50"),
        line("gone-from-the-menu", 1, "10.00")], delivery="5.00")
    s = await one(session, order)
    # 2 x 8.16 + 6.20, and 60% of the $10 dish that has no PPP any more.
    assert s.kitchen == D("16.32") + D("6.20") + D("6.00")
    assert s.company == s.paid - s.kitchen - D("5.00")
    shares = {l["retailer_id"]: (l["ppp"], l["share"]) for l in s.lines}
    assert shares[DRUMSTICK] == ("8.16", "16.32") and shares["gone-from-the-menu"] == (None, "6.00")
    assert s.rule.startswith("Partner price (PPP)")


async def test_coupons_and_points_come_out_of_sheros_share_or_both(session, customer, outlet, menu):
    off_food = await promo(session, DiscountType.PERCENT)
    order = await paid(session, customer, outlet, [line(DRUMSTICK, 8, "12.50")],
                       coupon="10.00", promotion=off_food)
    s = await one(session, order)
    assert (s.paid, s.kitchen, s.company) == (D("98.00"), D("65.00"), D("25.00"))

    await save(session, discounts="shared")
    s = await one(session, order)
    assert (s.kitchen, s.company) == (D("58.50"), D("31.50"))

    points = await paid(session, customer, outlet, [line(DRUMSTICK, 8, "12.50")], points="5.00")
    assert (await one(session, points)).kitchen == D("61.75")

    # A free-delivery coupon is never the kitchen's to give.
    free = await promo(session, DiscountType.FREE_DELIVERY)
    delivery = await paid(session, customer, outlet, [line(DRUMSTICK, 8, "12.50")],
                          coupon="8.00", promotion=free)
    s = await one(session, delivery)
    assert (s.paid, s.kitchen, s.company) == (D("100.00"), D("65.00"), D("27.00"))
    # Still read right after the promotion is deleted.
    await session.delete(free)
    await session.flush()
    delivery.promotion_id = None
    assert (await one(session, delivery)).kitchen == D("65.00")


async def test_a_kitchen_can_have_its_own_split(session, customer, outlet, far_outlet, menu):
    await save(session, kitchens=[{"outlet_id": str(far_outlet.id), "percent": "70"}])
    here = await paid(session, customer, outlet, [line(DRUMSTICK, 8, "12.50")])
    there = await paid(session, customer, far_outlet, [line(DRUMSTICK, 8, "12.50")])
    assert (await one(session, here)).kitchen == D("65.00")
    assert (await one(session, there)).kitchen == D("70.00")

    rules = await save(session, kitchens=[{"outlet_id": str(far_outlet.id), "basis": "dish_price"}])
    assert rules.rule_for(far_outlet.id) == ("dish_price", D("65"))
    assert (await one(session, there)).kitchen == D("65.28")             # 8 x 8.16
    # A row that changes nothing is not kept.
    assert (await save(session, kitchens=[{"outlet_id": str(far_outlet.id)}])).kitchens == []


async def test_a_paid_order_keeps_the_split_it_was_paid_under(session, customer, outlet, menu):
    order = await paid(session, customer, outlet, [line(DRUMSTICK, 8, "12.50")])
    await payouts.lock(session, order)
    later = await paid(session, customer, outlet, [line(DRUMSTICK, 8, "12.50")])
    await save(session, kitchen_percent="50")
    assert (await one(session, order)).kitchen == D("65.00")
    s = await one(session, later)
    assert s.kitchen == D("50.00") and not s.locked

    await payouts.set_amount(session, later, "44", "agreed", "ops@test")
    assert await payouts.apply_rules(session, [order, later], "ops@test") == 2
    await save(session, kitchen_percent="40")
    assert (await one(session, order)).kitchen == D("50.00")             # copied at 50%
    s = await one(session, later)
    assert s.locked and s.kitchen == D("44.00") and s.manual["note"] == "agreed"


async def test_refunded_and_cancelled_orders_owe_the_kitchen_nothing(session, customer, outlet, menu):
    order = await paid(session, customer, outlet, [line(DRUMSTICK, 8, "12.50")])
    order.payment_status, order.refund_amount = PaymentStatus.REFUNDED, D("108.00")
    order.stage = OrderStage.REFUNDED
    s = await one(session, order)
    assert (s.kitchen, s.company, s.status) == (D("0.00"), D("0.00"), "none")
    # The courier already went: Shero still pays Uber.
    order.uber_delivery_id = "del_1"
    assert (await one(session, order)).company == D("-8.00")
    # ...unless the team says the kitchen is owed for what it cooked.
    await payouts.set_amount(session, order, "30", "cooked before the refund", "ops@test")
    s = await one(session, order)
    assert (s.kitchen, s.status) == (D("30.00"), "due")


async def test_a_hand_set_amount_is_checked(session, customer, outlet, menu):
    order = await paid(session, customer, outlet, [line(DRUMSTICK, 8, "12.50")])
    for bad in ("-1", "abc", "500"):
        with pytest.raises(payouts.PayoutError):
            await payouts.set_amount(session, order, bad, "", "ops@test")
    s = await payouts.set_amount(session, order, "$70.5", "", "ops@test")
    assert (s.kitchen, s.company) == (D("70.50"), D("29.50"))
    s = await payouts.set_amount(session, order, "", "", "ops@test")
    assert s.kitchen == D("65.00") and s.manual is None

    unpaid = await paid(session, customer, outlet, [line(DRUMSTICK, 1, "12.50")])
    unpaid.payment_status = PaymentStatus.LINK_SENT
    with pytest.raises(payouts.PayoutError):
        await payouts.set_amount(session, unpaid, "5", "", "ops@test")


# --- Paying kitchens ---------------------------------------------------------------
async def test_paying_a_kitchen_and_undoing_it(session, customer, outlet, far_outlet, menu):
    delivered = OrderStage.DELIVERED
    a = await paid(session, customer, outlet, [line(DRUMSTICK, 8, "12.50")], stage=delivered)
    b = await paid(session, customer, outlet, [line(RASAM, 2, "9.50")], stage=delivered)
    waiting = await paid(session, customer, outlet, [line(RASAM, 1, "9.50")])
    elsewhere = await paid(session, customer, far_outlet, [line(RASAM, 1, "9.50")], stage=delivered)
    items = await payouts.splits(session, [a, b, waiting, elsewhere])
    t = payouts.by_kitchen(items)[outlet.id]
    assert (t.due, t.due_orders, t.upcoming) == (D("77.35"), 2, D("6.18"))

    payout = await payouts.pay(session, outlet, items, paid_on=date(2026, 10, 30),
                               reference="Zelle 4821", note="", actor="ops@test")
    assert (payout.amount, payout.order_count) == (D("77.35"), 2)
    assert a.payout_id == b.payout_id == payout.id and waiting.payout_id is None
    assert elsewhere.payout_id is None
    assert a.payout_split["paid"] == "65.00" and "gross" in b.payout_split

    # A paid-out order cannot be changed, and a rule change does not touch it.
    with pytest.raises(payouts.PayoutError):
        await payouts.set_amount(session, a, "1", "", "ops@test")
    await save(session, kitchen_percent="10")
    s = await one(session, a)
    assert (s.status, s.kitchen, s.paid_out) == ("paid_out", D("65.00"), D("65.00"))
    with pytest.raises(payouts.PayoutError):
        await payouts.pay(session, outlet, await payouts.splits(session, [a, b]),
                          paid_on=date.today(), reference="", note="", actor="ops@test")

    # Refunded after the kitchen was paid: flagged to get back.
    a.payment_status, a.refund_amount = PaymentStatus.REFUNDED, D("108.00")
    s = await one(session, a)
    assert s.status == "recover" and payouts.totals([s]).recover == D("65.00")

    assert await payouts.undo(session, payout, "ops@test") == 2
    assert payout.voided_at and payout.voided_by == "ops@test"
    assert a.payout_id is None and "paid" not in a.payout_split
    assert (await one(session, b)).status == "due"
    with pytest.raises(payouts.PayoutError):
        await payouts.undo(session, payout, "ops@test")


async def test_orders_by_payment_date_and_the_statement(session, customer, outlet, menu):
    now = datetime.now(timezone.utc)
    old = await paid(session, customer, outlet, [line(DRUMSTICK, 1, "12.50")],
                     paid_at=now - timedelta(days=40))
    new = await paid(session, customer, outlet, [line(DRUMSTICK, 8, "12.50")])
    unpaid = await paid(session, customer, outlet, [line(DRUMSTICK, 1, "12.50")])
    unpaid.payment_status = PaymentStatus.EXPIRED
    await session.flush()
    found = await payouts.orders_between(session, now - timedelta(days=7), None, outlet.id)
    assert [o.order_number for o in found] == [new.order_number]
    assert len(await payouts.orders_between(session, None, None)) == 2

    text = payouts.statement_csv(await payouts.splits(session, [new, old]), {outlet.id: outlet.name})
    rows = text.lstrip("﻿").splitlines()
    assert rows[0].startswith("Order,Paid at (UTC),Kitchen")
    assert new.order_number in rows[1] and "8 x Drumstick" in rows[1] and ",65.00,35.00," in rows[1]
    assert rows[-1].startswith("Total,") and "2 orders" in rows[-1]


async def test_ppp_coverage(session, menu):
    cover = await payouts.ppp_coverage(session)
    assert cover["dishes"] == cover["with_ppp"] == 4
    assert D("65.0") < cover["average_percent"] < D("65.5")


# --- Rules ------------------------------------------------------------------------
async def test_split_rules_are_checked(session, outlet):
    for bad in ({"kitchen_percent": "101"}, {"kitchen_percent": "-1"},
                {"kitchen_percent": "65.123"}, {"basis": "other"},
                {"kitchens": [{"outlet_id": str(outlet.id), "percent": "5"},
                              {"outlet_id": str(outlet.id), "percent": "6"}]}):
        with pytest.raises(ValueError):
            await save(session, **bad)
    with pytest.raises(payouts.PayoutError):
        await save(session, kitchens=[{"outlet_id": "6f1c1c6e-9a52-4a42-9d8e-3c1b9d3f0a11",
                                       "percent": "5"}])
    rules = await save(session, kitchen_percent="62.5")
    assert (await payouts.rules(session)).kitchen_percent == D("62.50")
    assert payouts.describe(rules.basis, rules.kitchen_percent) == "62.5% of the food"
    assert payouts.describe("percent", D("100.00")) == "100% of the food"


async def test_payment_copies_the_split_onto_the_order(session, customer, outlet, menu, monkeypatch):
    from app.integrations.gallabox.sender import use_sender
    from app.services import payments

    async def nothing(*args, **kwargs):
        return True

    for target, name in ((payments, "send_payment_confirmation"),
                         (payments.crm_sync, "convert_and_record_order"),
                         (payments.order_alerts, "notify_new_order"),
                         (payments.kitchen_alerts, "send_alert")):
        monkeypatch.setattr(target, name, nothing)
    order = await paid(session, customer, outlet, [line(DRUMSTICK, 8, "12.50")])
    order.payment_status, order.stage, order.paid_at = (PaymentStatus.LINK_SENT,
                                                       OrderStage.PENDING_PAYMENT, None)

    class Quiet:
        async def send_template(self, *a, **kw):
            return None

    with use_sender(Quiet()):
        assert await payments.handle_payment_success(session, order, customer)
    assert order.payout_split["gross"] == "65.00" and order.payout_split["locked_by"] == "payment"
    await save(session, kitchen_percent="50")
    assert (await one(session, order)).kitchen == D("65.00")
