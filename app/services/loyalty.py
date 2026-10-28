"""Transactional points, policy snapshots, reservations and refund reversals.

Balances may become negative only when a paid/refunded order has to be honoured
and its original points were already spent. Future earnings repay that debt.
"""
from __future__ import annotations
import json
import uuid
from datetime import date
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, update
from app.core.exceptions import BotFlowError
from app.db.models import AppSetting, Customer, LoyaltyAccount, LoyaltyEntry, Order, PaymentStatus

KEY = "LOYALTY_PROGRAM"
CENT = Decimal("0.01")

class LoyaltyError(BotFlowError):
    def __init__(self, message):
        super().__init__(message, customer_message=message)

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

class DishBonus(BaseModel):
    """Extra points for each one of a dish ordered."""
    model_config = ConfigDict(extra="forbid", validate_default=True)
    retailer_id: str = Field(min_length=1, max_length=120)
    points: int = Field(ge=1, le=100000)

class Tier(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)
    name: str = Field(min_length=1, max_length=40)
    orders: int = Field(ge=1, le=1000000)
    multiplier: Decimal = Field(ge=1, le=20)

class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)
    enabled: bool = Field(False, description="Enable the loyalty program")
    name: str = Field("Shero Rewards", min_length=1, max_length=60, description="Program name")
    currency: str = Field("USD", pattern="^[A-Z]{3}$", description="Program currency (matches order currency)")
    earning_enabled: bool = Field(True, description="Allow earning points")
    redemption_enabled: bool = Field(True, description="Allow spending points")
    points_per_currency: Decimal = Field(1, ge=0, le=1000, description="Points per currency unit of eligible food spend")
    points_per_order: int = Field(0, ge=0, le=1000000, description="Extra points per eligible order")
    minimum_earn_subtotal: Decimal = Field(10, ge=0, le=1000000, description="Minimum eligible food subtotal to earn")
    max_earn_per_order: int = Field(0, ge=0, le=1000000, description="Maximum points earned per order (0 = no cap)")
    first_order_bonus: int = Field(25, ge=0, le=1000000, description="Bonus on first eligible program order")
    repeat_order_bonus: int = Field(0, ge=0, le=1000000, description="Bonus on each repeat eligible order")
    repeat_from_order: int = Field(2, ge=2, le=1000000, description="First eligible order number receiving repeat bonus")
    milestone_every: int = Field(5, ge=0, le=1000000, description="Award milestone every N eligible orders (0 = off)")
    milestone_bonus: int = Field(50, ge=0, le=1000000, description="Points per milestone")
    point_value: Decimal = Field(Decimal("0.01"), ge=Decimal("0.01"), le=100, decimal_places=2, description="Discount value of one point")
    minimum_redeem_points: int = Field(100, ge=1, le=1000000, description="Minimum points per redemption")
    redeem_step: int = Field(1, ge=1, le=1000000, description="Spend points in multiples of this number")
    minimum_redeem_subtotal: Decimal = Field(10, ge=0, le=1000000, description="Minimum eligible food subtotal to redeem")
    maximum_redeem_percent: Decimal = Field(25, gt=0, le=100, description="Maximum percent of eligible food covered by points")
    maximum_redeem_amount: Decimal = Field(10, ge=0, le=1000000, description="Maximum loyalty discount per order (0 = no fixed cap)")
    big_order_threshold: Decimal = Field(0, ge=0, le=1000000, description="Big-order bonus: eligible food of at least (0 = off)")
    big_order_bonus: int = Field(0, ge=0, le=1000000, description="Big-order bonus points")
    promo_multiplier: Decimal = Field(1, ge=1, le=10, description="Bonus-points promotion: multiply spend points by")
    promo_starts_on: date | None = Field(None, description="Promotion starts on (blank = already running)")
    promo_ends_on: date | None = Field(None, description="Promotion ends on (blank = no end)")
    promo_days: list[str] = Field(default_factory=list, description="Promotion days of the week (none = every day)")
    dish_bonuses: list[DishBonus] = Field(default_factory=list, description="Extra points per dish ordered")
    stack_with_coupons: bool = Field(False, description="Allow points and a coupon on the same order")
    earn_with_coupons: bool = Field(True, description="Earn points on coupon orders")
    earn_on_redemption: bool = Field(True, description="Earn on remaining food spend when using points")
    kitchen_ids: list[uuid.UUID] = Field(default_factory=list, description="Eligible kitchen IDs (empty = all)")
    excluded_items: list[str] = Field(default_factory=list, description="Retailer IDs excluded from earning and redemption")
    tiers: list[Tier] = Field(default_factory=lambda: [Tier(name="Silver", orders=5, multiplier=Decimal("1.5")), Tier(name="Gold", orders=10, multiplier=2)], description="Membership tiers: name, eligible order threshold, earning multiplier")

    @field_validator("points_per_currency", "minimum_earn_subtotal", "point_value", "minimum_redeem_subtotal", "maximum_redeem_percent", "maximum_redeem_amount")
    @classmethod
    def finite(cls, value):
        if not value.is_finite():
            raise ValueError("Use a finite number")
        return value

    @model_validator(mode="after")
    def consistent(self):
        if self.maximum_redeem_amount and self.minimum_redeem_points * self.point_value > self.maximum_redeem_amount:
            raise ValueError("Minimum redemption exceeds the fixed discount cap; lower the minimum or raise the cap")
        if self.minimum_redeem_points % self.redeem_step:
            raise ValueError("Minimum redeem points must be a multiple of the redemption step")
        if len(self.tiers) > 20 or len({t.orders for t in self.tiers}) != len(self.tiers):
            raise ValueError("Use at most 20 tiers, each with a different order threshold")
        if len(self.excluded_items) > 500 or len(self.kitchen_ids) > 500:
            raise ValueError("Too many item or kitchen rules")
        if self.promo_starts_on and self.promo_ends_on and self.promo_ends_on < self.promo_starts_on:
            raise ValueError("The promotion ends before it starts")
        if any(day not in DAYS for day in self.promo_days):
            raise ValueError("Promotion days must be mon, tue, wed, thu, fri, sat or sun")
        if len(self.dish_bonuses) > 500 or len({b.retailer_id for b in self.dish_bonuses}) != len(self.dish_bonuses):
            raise ValueError("Give each dish one bonus (at most 500 dishes)")
        if self.big_order_bonus and not self.big_order_threshold:
            raise ValueError("Set the food amount a big-order bonus needs")
        return self

async def policy(session) -> Policy:
    row = (await session.execute(select(AppSetting).where(AppSetting.key == KEY))).scalar_one_or_none()
    return Policy.model_validate_json(row.value) if row and row.value else Policy()

async def save_policy(session, data: dict, actor: str) -> Policy:
    rules = Policy.model_validate(data)
    if rules.kitchen_ids:
        from app.db.models import Outlet
        found = set((await session.execute(select(Outlet.id).where(Outlet.id.in_(rules.kitchen_ids)))).scalars())
        if found != set(rules.kitchen_ids):
            raise ValueError("One or more kitchen IDs do not exist")
    row = (await session.execute(select(AppSetting).where(AppSetting.key == KEY).with_for_update())).scalar_one_or_none()
    if row is None:
        row = AppSetting(key=KEY, is_secret=False)
        session.add(row)
    row.value, row.updated_by = rules.model_dump_json(), actor
    await session.flush()
    return rules

async def account(session, customer_id, *, lock=False):
    if lock:
        # The insert also takes SQLite's writer lock; a SELECT FOR UPDATE
        # alone would be ignored there. On PostgreSQL the customer row orders
        # concurrent first-wallet creation before checking event keys.
        await session.execute(select(Customer.id).where(Customer.id == customer_id).with_for_update())
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        elif dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            insert = None
        if insert:
            await session.execute(insert(LoyaltyAccount).values(customer_id=customer_id, balance=0, eligible_orders=0)
                                  .on_conflict_do_nothing(index_elements=["customer_id"]))
    query = select(LoyaltyAccount).where(LoyaltyAccount.customer_id == customer_id)
    if lock:
        query = query.with_for_update()
    row = (await session.execute(query.execution_options(populate_existing=True))).scalar_one_or_none()
    if row is None and lock:
        row = LoyaltyAccount(customer_id=customer_id, balance=0, eligible_orders=0)
        session.add(row)
        await session.flush()
    return row

async def _exists(session, event_key):
    return await session.scalar(select(LoyaltyEntry.id).where(LoyaltyEntry.event_key == event_key)) is not None

async def _change(session, wallet, points, kind, key, *, order=None, note="", actor=None, require_funds=False):
    stmt = update(LoyaltyAccount).where(LoyaltyAccount.customer_id == wallet.customer_id)
    if require_funds:
        stmt = stmt.where(LoyaltyAccount.balance >= -points)
    balance = (await session.execute(stmt.values(balance=LoyaltyAccount.balance + points)
                                     .returning(LoyaltyAccount.balance))).scalar_one_or_none()
    if balance is None:
        raise LoyaltyError("Your points balance changed. Please refresh the total and try again.")
    session.add(LoyaltyEntry(customer_id=wallet.customer_id, order_id=order.id if order else None,
        event_key=key, kind=kind, points=points, balance_after=balance, note=note, actor=actor))
    await session.flush()
    return balance

def promo_active(rules, day: date) -> bool:
    """Whether the bonus-points promotion runs on this day."""
    return (rules.promo_multiplier > 1
            and (rules.promo_starts_on is None or day >= rules.promo_starts_on)
            and (rules.promo_ends_on is None or day <= rules.promo_ends_on)
            and (not rules.promo_days or DAYS[day.weekday()] in rules.promo_days))

def dish_bonus(lines, rules) -> int:
    """Extra points for the bonus dishes in these cart lines."""
    per_dish = {b.retailer_id: b.points for b in rules.dish_bonuses}
    return sum(per_dish[line.retailer_id] * line.quantity for line in lines
               if line.retailer_id in per_dish and line.retailer_id not in rules.excluded_items)

def tier_for(rules, number):
    """The tier an eligible order with this number falls in, or None."""
    found = None
    for tier in sorted(rules.tiers, key=lambda t: t.orders):
        if number >= tier.orders:
            found = tier
    return found

def earn_points(rules, *, spend, subtotal, number, promo=False, dish_points=0) -> int:
    """Points for the customer's `number`th eligible order: `spend` is the
    food they paid for, `subtotal` the eligible food before discounts. Used
    when an order is paid, and for the examples on the admin page."""
    tier = tier_for(rules, number)
    multiplier = (tier.multiplier if tier else Decimal(1)) * (rules.promo_multiplier if promo else 1)
    points = int((spend * rules.points_per_currency * multiplier).to_integral_value(rounding=ROUND_FLOOR)) + rules.points_per_order
    if number == 1:
        points += rules.first_order_bonus
    if number >= rules.repeat_from_order:
        points += rules.repeat_order_bonus
    if rules.milestone_every and number % rules.milestone_every == 0:
        points += rules.milestone_bonus
    if rules.big_order_threshold and subtotal >= rules.big_order_threshold:
        points += rules.big_order_bonus
    points += dish_points
    if rules.max_earn_per_order:
        points = min(points, rules.max_earn_per_order)
    return points

def eligible_subtotal(priced, rules):
    return sum((line.line_total for line in priced.lines if line.retailer_id not in rules.excluded_items), Decimal(0))

def eligible(rules, currency, outlet_id):
    return rules.enabled and rules.currency == currency.upper() and (not rules.kitchen_ids or outlet_id in rules.kitchen_ids)

def food_after_coupon(priced):
    from app.services import promotions
    coupon = priced.discount if promotions.comes_off_dishes(priced.promotion) else Decimal(0)
    return max(Decimal(0), priced.dish_total - coupon)

async def apply_to_quote(session, priced, *, customer_id, outlet_id, requested=0):
    from app.services.pricing import _settle, money
    rules = await policy(session)
    priced.loyalty_policy = rules.model_dump(mode="json")
    priced.loyalty_points = 0
    priced.loyalty_discount = Decimal("0.00")
    # Strict validation; never accept decimals, booleans or unbounded integers.
    if isinstance(requested, bool) or not str(requested).isdigit() or int(requested) > 1000000:
        raise LoyaltyError("Enter a whole number of points between 0 and 1000000.")
    requested = int(requested)
    wallet = await account(session, customer_id)
    balance = wallet.balance if wallet else 0
    usable = eligible(rules, priced.currency, outlet_id)
    subtotal = eligible_subtotal(priced, rules)
    # Allocate coupon reductions proportionally to eligible food.
    food = food_after_coupon(priced)
    available_food = subtotal * food / priced.dish_total if priced.dish_total else Decimal(0)
    cap = available_food * rules.maximum_redeem_percent / 100
    if rules.maximum_redeem_amount:
        cap = min(cap, rules.maximum_redeem_amount)
    max_points = max(0, min(1000000, balance, int(cap / rules.point_value)))
    max_points -= max_points % rules.redeem_step
    if (not usable or not rules.redemption_enabled or subtotal < rules.minimum_redeem_subtotal
            or (priced.discount and not rules.stack_with_coupons)):
        max_points = 0
    # Keep Stripe's minimum even after the loyalty reduction lowers food tax.
    low, high = 0, max_points // rules.redeem_step
    while low < high:
        mid = (low + high + 1) // 2
        priced.loyalty_discount = money(mid * rules.redeem_step * rules.point_value)
        _settle(priced)
        if priced.total >= Decimal("0.50"):
            low = mid
        else:
            high = mid - 1
    max_points = low * rules.redeem_step
    if max_points < rules.minimum_redeem_points:
        max_points = 0
    if requested and (requested < rules.minimum_redeem_points or requested % rules.redeem_step or requested > max_points):
        raise LoyaltyError(f"You can redeem up to {max_points} points here, in steps of {rules.redeem_step} (minimum {rules.minimum_redeem_points}). Check your balance, coupon and order eligibility.")
    priced.loyalty_points = requested
    priced.loyalty_discount = money(requested * rules.point_value)
    _settle(priced)
    # Store eligible food spend at the point the total was agreed.
    priced.loyalty_policy["eligible_subtotal"] = str(subtotal)
    priced.loyalty_policy["eligible_net_spend"] = str(max(Decimal(0), available_food - priced.loyalty_discount))
    priced.loyalty_policy["eligible_kitchen"] = usable
    # The promotion and dish bonuses are judged now too, so a payment made
    # tomorrow still gets the points the customer was offered today.
    if rules.promo_multiplier > 1:
        from app.db.models import Outlet
        from app.services import promotions
        outlet = await session.get(Outlet, outlet_id) if outlet_id else None
        priced.loyalty_policy["eligible_promo"] = promo_active(rules, await promotions.local_today(session, outlet))
    priced.loyalty_policy["eligible_dish_bonus"] = dish_bonus(priced.lines, rules)
    return {"enabled": rules.enabled, "name": rules.name, "balance": balance,
            "max_points": max_points, "minimum_points": rules.minimum_redeem_points,
            "step": rules.redeem_step, "point_value": str(rules.point_value),
            "selected_points": requested, "discount": str(priced.loyalty_discount),
            "currency": rules.currency}

async def reserve(session, order):
    if not order.loyalty_points:
        return
    wallet = await account(session, order.customer_id, lock=True)
    key = f"hold:{order.id}"
    if await _exists(session, key):
        return
    await _change(session, wallet, -order.loyalty_points, "hold", key, order=order,
                  require_funds=True, note="Points reserved while payment is pending")
    order.loyalty_state = "held"
    await session.flush()

async def release(session, order, *, reason):
    if not order.loyalty_points:
        return
    wallet = await account(session, order.customer_id, lock=True)
    await session.refresh(order, attribute_names=["loyalty_state"])
    if order.loyalty_state != "held":
        return
    await _change(session, wallet, order.loyalty_points, "release", f"release:{order.id}", order=order, note=reason)
    order.loyalty_state = "released"
    await session.flush()

async def paid(session, order):
    if order.payment_status != PaymentStatus.PAID or not order.loyalty_policy or not order.loyalty_policy.get("enabled"):
        return 0  # Never award retroactively to pre-program orders.
    wallet = await account(session, order.customer_id, lock=True)
    key = f"paid:{order.id}"
    if await _exists(session, key):
        return order.loyalty_earned
    await session.refresh(order, attribute_names=["loyalty_state"])
    if order.loyalty_points and order.loyalty_state == "released":
        # A late payment must still pay for its discount. Debt is repaid by
        # future earnings if released points have already been spent elsewhere.
        await _change(session, wallet, -order.loyalty_points, "late_redeem", f"late:{order.id}", order=order,
                      note="Payment received after the points reservation was released")
    if order.loyalty_points:
        order.loyalty_state = "spent"
    data = dict(order.loyalty_policy)
    subtotal = Decimal(data.pop("eligible_subtotal", "0"))
    spend = Decimal(data.pop("eligible_net_spend", "0"))
    usable = data.pop("eligible_kitchen", False)
    promo = data.pop("eligible_promo", False)
    dish_points = int(data.pop("eligible_dish_bonus", 0))
    rules = Policy.model_validate(data)
    qualifies = (usable and rules.earning_enabled and subtotal > 0 and subtotal >= rules.minimum_earn_subtotal
                 and (rules.earn_with_coupons or not order.promo_code)
                 and (rules.earn_on_redemption or not order.loyalty_points))
    points = 0
    if qualifies:
        points = earn_points(rules, spend=spend, subtotal=subtotal, number=wallet.eligible_orders + 1,
                             promo=promo, dish_points=dish_points)
        await session.execute(update(LoyaltyAccount).where(LoyaltyAccount.customer_id == order.customer_id)
                              .values(eligible_orders=LoyaltyAccount.eligible_orders + 1))
    order.loyalty_earned = points
    order.loyalty_policy = {**order.loyalty_policy, "counted": qualifies}
    await _change(session, wallet, points, "earn", key, order=order, note="Eligible paid order" if qualifies else "Order not eligible for earning")
    await session.flush()
    return points

async def refund(session, order):
    if not order.loyalty_policy:
        return
    wallet = await account(session, order.customer_id, lock=True)
    await session.refresh(order, attribute_names=["loyalty_policy", "loyalty_state"])
    key = f"refund:{order.id}:{Decimal(order.refund_amount or 0):.2f}"
    if await _exists(session, key):
        return
    ratio = min(Decimal(1), max(Decimal(0), Decimal(order.refund_amount or 0) / order.total)) if order.total else Decimal(1)
    data = dict(order.loyalty_policy)
    returned = int((Decimal(order.loyalty_points) * ratio).to_integral_value(rounding=ROUND_FLOOR)) if order.loyalty_state in {"spent", "refunded"} else 0
    removed = int((Decimal(order.loyalty_earned) * ratio).to_integral_value(rounding=ROUND_HALF_UP))
    old_returned, old_removed = data.get("refund_returned", 0), data.get("refund_removed", 0)
    if returned < old_returned or removed < old_removed:
        return  # Ignore stale, out-of-order cumulative refund events.
    if ratio == 1 and data.get("counted") and not data.get("refund_count_reversed"):
        await session.execute(update(LoyaltyAccount).where(LoyaltyAccount.customer_id == order.customer_id)
                              .values(eligible_orders=LoyaltyAccount.eligible_orders - 1))
        data["refund_count_reversed"] = True
    await _change(session, wallet, (returned - old_returned) - (removed - old_removed), "refund", key, order=order,
                  note=f"Returned {returned - old_returned} redeemed points; reversed {removed - old_removed} earned points")
    order.loyalty_policy = {**data, "refund_returned": returned, "refund_removed": removed}
    if ratio == 1:
        order.loyalty_state = "refunded"
    await session.flush()


async def adjust(session, customer_id, points, *, note, actor, request_id):
    if not isinstance(points, int) or isinstance(points, bool) or not -1000000 <= points <= 1000000 or not points:
        raise ValueError("Adjustment must be a non-zero whole number between -1000000 and 1000000")
    if not note.strip() or len(note) > 500:
        raise ValueError("Enter an adjustment reason (up to 500 characters)")
    # UUID makes duplicate form submissions idempotent without trusting a user-supplied ledger key.
    key = f"adjust:{uuid.UUID(str(request_id))}"
    wallet = await account(session, customer_id, lock=True)
    if not await _exists(session, key):
        await _change(session, wallet, points, "adjustment", key, note=note, actor=actor, require_funds=points < 0)

async def reset_all(session, *, note, actor) -> tuple[int, int]:
    """Every balance to zero, each as an adjustment in that customer's
    history. Returns (wallets changed, points removed)."""
    wallets = list((await session.execute(select(LoyaltyAccount).where(LoyaltyAccount.balance != 0))).scalars())
    removed = 0
    for wallet in wallets:
        before = wallet.balance
        await adjust(session, wallet.customer_id, -before, note=note, actor=actor, request_id=uuid.uuid4())
        removed += before
    return len(wallets), removed

async def overview(session, customer_id):
    rules = await policy(session)
    wallet = await account(session, customer_id)
    count = wallet.eligible_orders if wallet else 0
    tier = tier_for(rules, count)
    tier_name = tier.name if tier else "Member"
    entries = list((await session.execute(select(LoyaltyEntry).where(LoyaltyEntry.customer_id == customer_id)
                   .order_by(LoyaltyEntry.created_at.desc(), LoyaltyEntry.id.desc()).limit(30))).scalars())
    return {"enabled": rules.enabled, "name": rules.name, "balance": wallet.balance if wallet else 0,
            "eligible_orders": count, "tier": tier_name, "point_value": str(rules.point_value),
            "currency": rules.currency, "entries": entries}
