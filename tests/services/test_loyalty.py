"""Loyalty financial lifecycle: real database, no external services."""
import uuid
from decimal import Decimal
import pytest
from sqlalchemy import func, select
from app.core.config import settings
from app.db.models import LoyaltyAccount, LoyaltyEntry, Order, PaymentStatus, OrderStage, Promotion
from app.services import loyalty, orders
from app.services.pricing import PricedLine, PricedOrder, _settle

async def configure(session, **changes):
    data = loyalty.Policy(enabled=True).model_dump(mode="json")
    data.update(changes)
    return await loyalty.save_policy(session, data, "admin@test.local")

async def grant(session, customer, points=1000):
    await loyalty.adjust(session, customer.id, points, note="Opening balance", actor="admin@test.local", request_id=uuid.uuid4())

async def quote(session, customer, outlet, points=0, subtotal="100", **kwargs):
    priced = PricedOrder(lines=[PricedLine("dish", "Sambar", 1, Decimal(subtotal), Decimal(subtotal))],
                        dish_total=Decimal(subtotal), currency="USD", **kwargs)
    _settle(priced)
    priced.loyalty_view = await loyalty.apply_to_quote(session, priced, customer_id=customer.id,
                                                       outlet_id=outlet.id, requested=points)
    return priced

async def draft(session, customer, outlet, points=0, subtotal="100"):
    priced = await quote(session, customer, outlet, points, subtotal)
    return await orders.create_draft_order(session, customer=customer, outlet=outlet, priced=priced)

async def pay(session, order):
    order.payment_status = PaymentStatus.PAID
    order.stage = OrderStage.PAID_SLOT_BOOKED
    return await loyalty.paid(session, order)

async def balance(session, customer):
    wallet = await loyalty.account(session, customer.id)
    return wallet.balance if wallet else 0

async def test_disabled_program_does_not_earn_or_redeem(session, customer, outlet):
    order = await draft(session, customer, outlet)
    assert await pay(session, order) == 0
    assert await balance(session, customer) == 0
    with pytest.raises(loyalty.LoyaltyError):
        await quote(session, customer, outlet, 100)

async def test_earn_once_and_discounted_food_only(session, customer, outlet):
    await configure(session)
    order = await draft(session, customer, outlet)
    assert await pay(session, order) == 125
    assert await pay(session, order) == 125
    assert await balance(session, customer) == 125
    assert (await loyalty.overview(session, customer.id))["eligible_orders"] == 1
    assert await session.scalar(select(func.count(LoyaltyEntry.id)).where(LoyaltyEntry.kind == "earn")) == 1

async def test_redeem_hold_release_and_duplicate_release(session, customer, outlet):
    await configure(session)
    await grant(session, customer)
    order = await draft(session, customer, outlet, 200)
    assert order.discount == order.loyalty_discount == Decimal("2")
    assert await balance(session, customer) == 800
    await loyalty.release(session, order, reason="cancelled")
    await loyalty.release(session, order, reason="cancelled")
    assert await balance(session, customer) == 1000
    assert order.loyalty_state == "released"

async def test_second_checkout_cannot_spend_reserved_points(session, customer, outlet):
    await configure(session)
    await grant(session, customer, 200)
    first_quote = await quote(session, customer, outlet, 200)
    stale_quote = await quote(session, customer, outlet, 200)
    first = await orders.create_draft_order(session, customer=customer, outlet=outlet, priced=first_quote)
    with pytest.raises(loyalty.LoyaltyError):
        await orders.create_draft_order(session, customer=customer, outlet=outlet, priced=stale_quote)
    assert await balance(session, customer) == 0
    assert (await session.execute(select(Order))).scalars().all() == [first]

async def test_repeat_milestone_and_tiers(session, customer, outlet):
    await configure(session, first_order_bonus=5, repeat_order_bonus=10, milestone_every=2,
                    milestone_bonus=50, tiers=[{"name":"Silver","orders":2,"multiplier":"2"}])
    first = await draft(session, customer, outlet, subtotal="20")
    second = await draft(session, customer, outlet, subtotal="20")
    assert await pay(session, first) == 25
    assert await pay(session, second) == 100  # 40 spend + 10 repeat + 50 milestone
    info = await loyalty.overview(session, customer.id)
    assert info["balance"] == 125 and info["tier"] == "Silver"

async def test_policy_snapshot_survives_admin_changes(session, customer, outlet):
    await configure(session, points_per_currency="2", first_order_bonus=0)
    order = await draft(session, customer, outlet, subtotal="20")
    await configure(session, enabled=False, points_per_currency="9", first_order_bonus=0)
    assert await pay(session, order) == 40

async def test_minimum_and_earn_cap(session, customer, outlet):
    await configure(session, minimum_earn_subtotal="30", max_earn_per_order=10)
    small = await draft(session, customer, outlet, subtotal="20")
    assert await pay(session, small) == 0
    normal = await draft(session, customer, outlet)
    assert await pay(session, normal) == 10
    assert (await loyalty.overview(session, customer.id))["eligible_orders"] == 1

@pytest.mark.parametrize("value", [-1, True, 1.5, "NaN", "Infinity", None, {}, 1000001])
async def test_invalid_redemption_inputs(session, customer, outlet, value):
    await configure(session)
    with pytest.raises(loyalty.LoyaltyError):
        await quote(session, customer, outlet, value)

async def test_step_minimum_percentage_and_fixed_cap(session, customer, outlet):
    await configure(session, minimum_redeem_points=100, redeem_step=50,
                    maximum_redeem_percent="10", maximum_redeem_amount="3")
    await grant(session, customer)
    priced = await quote(session, customer, outlet)
    assert priced.loyalty_view["max_points"] == 300
    for points in [50, 125, 350]:
        with pytest.raises(loyalty.LoyaltyError):
            await quote(session, customer, outlet, points)
    priced = await quote(session, customer, outlet, 300)
    assert priced.loyalty_discount == Decimal("3")

async def test_stripe_minimum_and_tax_match(session, customer, outlet, monkeypatch):
    monkeypatch.setattr(settings, "tax_percent", 10)
    await configure(session, minimum_redeem_points=1, minimum_redeem_subtotal="0",
                    maximum_redeem_percent="100", maximum_redeem_amount="0")
    await grant(session, customer)
    priced = await quote(session, customer, outlet, subtotal="1")
    allowed = priced.loyalty_view["max_points"]
    priced = await quote(session, customer, outlet, allowed, subtotal="1")
    assert priced.total >= Decimal("0.50")
    assert priced.tax == ((priced.dish_total - priced.loyalty_discount) * Decimal(".1")).quantize(Decimal(".01"), rounding="ROUND_HALF_UP")
    with pytest.raises(loyalty.LoyaltyError):
        await quote(session, customer, outlet, allowed+1, subtotal="1")

async def test_exclusions_currency_and_kitchen_rules(session, customer, outlet, far_outlet):
    await configure(session, kitchen_ids=[str(far_outlet.id)])
    assert await pay(session, await draft(session, customer, outlet)) == 0
    await configure(session, currency="INR")
    assert await pay(session, await draft(session, customer, outlet)) == 0
    await configure(session, excluded_items=["dish"])
    assert await pay(session, await draft(session, customer, outlet)) == 0

async def test_coupon_stacking_and_earning_policy(session, customer, outlet):
    await configure(session, earn_with_coupons=False)
    await grant(session, customer)
    promo = Promotion(code="SAVE", title="Save", discount_type="flat", discount_value=Decimal("10"))
    session.add(promo)
    await session.flush()
    with pytest.raises(loyalty.LoyaltyError):
        await quote(session, customer, outlet, 100, discount=Decimal("10"), promotion=promo)
    await configure(session, stack_with_coupons=True, earn_with_coupons=False)
    priced = await quote(session, customer, outlet, 100, discount=Decimal("10"), promotion=promo)
    order = await orders.create_draft_order(session, customer=customer, outlet=outlet, priced=priced)
    assert order.discount == Decimal("11") and order.loyalty_discount == 1
    assert await pay(session, order) == 0

async def test_late_paid_redeems_again_and_creates_debt_if_needed(session, customer, outlet):
    await configure(session, earning_enabled=False)
    await grant(session, customer, 200)
    order = await draft(session, customer, outlet, 200)
    await loyalty.release(session, order, reason="expired")
    await draft(session, customer, outlet, 200)  # another payment holds the returned points
    assert await balance(session, customer) == 0
    await pay(session, order)
    await pay(session, order)
    assert await balance(session, customer) == -200

async def test_full_refund_restores_redemption_and_reverses_earnings_once(session, customer, outlet):
    await configure(session)
    await grant(session, customer)
    order = await draft(session, customer, outlet, 200)
    assert await pay(session, order) == 123  # 98 net food + 25 first bonus
    order.refund_amount = order.total
    await loyalty.refund(session, order)
    await loyalty.refund(session, order)
    assert await balance(session, customer) == 1000
    assert (await loyalty.overview(session, customer.id))["eligible_orders"] == 0

async def test_cumulative_partial_refunds_then_full(session, customer, outlet):
    await configure(session, first_order_bonus=0)
    await grant(session, customer)
    order = await draft(session, customer, outlet, 200)
    await pay(session, order)
    order.refund_amount = order.total / 2
    await loyalty.refund(session, order)
    assert await balance(session, customer) == 949  # 800 + 98 + 100 returned -49 reversed
    order.refund_amount = order.total
    await loyalty.refund(session, order)
    assert await balance(session, customer) == 1000
    assert (await loyalty.overview(session, customer.id))["eligible_orders"] == 0

async def test_adjustments_are_audited_and_duplicate_safe(session, customer):
    token = uuid.uuid4()
    for _ in range(2):
        await loyalty.adjust(session, customer.id, 100, note="Goodwill", actor="ops@test", request_id=token)
    assert await balance(session, customer) == 100
    entry = (await session.execute(select(LoyaltyEntry))).scalar_one()
    assert entry.actor == "ops@test" and entry.note == "Goodwill"
    with pytest.raises(loyalty.LoyaltyError):
        await loyalty.adjust(session, customer.id, -200, note="Correction", actor="ops@test", request_id=uuid.uuid4())

@pytest.mark.parametrize("change", [{"point_value":"0"}, {"points_per_currency":"NaN"},
    {"minimum_redeem_points":101, "redeem_step":50}, {"maximum_redeem_percent":101},
    {"unknown":True}, {"tiers":[{"name":"Bad","orders":0,"multiplier":1}]}])
async def test_invalid_policy_is_rejected(session, change):
    with pytest.raises(ValueError):
        await configure(session, **change)


async def test_refund_of_already_spent_earnings_creates_repayable_debt(session, customer, outlet):
    await configure(session, first_order_bonus=0)
    order = await draft(session, customer, outlet)
    await pay(session, order)
    assert await balance(session, customer) == 100
    await draft(session, customer, outlet, 100)
    order.refund_amount = order.total
    await loyalty.refund(session, order)
    assert await balance(session, customer) == -100
    with pytest.raises(loyalty.LoyaltyError):
        await quote(session, customer, outlet, 100)
    await pay(session, await draft(session, customer, outlet))
    assert await balance(session, customer) == 0


async def test_customer_deletion_removes_wallet_and_audit_rows(session, customer):
    from app.services.customer_admin import delete_customer
    await grant(session, customer)
    await delete_customer(session, customer)
    assert await session.scalar(select(func.count(LoyaltyEntry.id))) == 0
    assert await session.scalar(select(func.count(LoyaltyAccount.customer_id))) == 0
