"""Kitchen partner payouts: how an order's money is split.

A customer pays for an order; part of it is the kitchen partner's and the
rest is Shero's. The split is set on the admin Payouts page:

    Kitchen share   a percentage of the food (the default), or each dish's
                    partner price - the PPP column of the menu sheet - times
                    the quantity. Each kitchen can have its own rule.
    Discounts       coupons and points come out of Shero's share (default),
                    or out of both shares in proportion.
    Shero's share   what the customer paid, less the kitchen's share and the
                    delivery, Uber fees and tax that Shero passes on.

    $100 of food, $8 delivery, kitchen 65%:
        the customer pays 108 = kitchen 65 + delivery 8 + Shero 35

The rule and each dish's share are copied onto the order when it is paid
(`lock`), so changing the rule later does not change what is owed for orders
already paid. Orders paid before that are worked out with today's rule until
they are paid out, or until `apply_rules` copies the rule onto them.

An order is due to its kitchen once it is delivered (or once paid, if the rule
says so). A refunded or cancelled order owes the kitchen nothing unless an
amount is set by hand. Paying a kitchen links its due orders to one
PartnerPayout and records the amount each was paid.
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select

from app.db.models import (AppSetting, MenuItem, Order, OrderStage, Outlet, PartnerPayout,
                           PaymentStatus, Promotion)

KEY = "PARTNER_SPLIT"
CENT = Decimal("0.01")
ZERO = Decimal("0.00")
HUNDRED = Decimal(100)
# The most a hand-set kitchen amount, a percentage and a note can hold.
NOTE_MAX = 500
REFERENCE_MAX = 120

Basis = Literal["percent", "dish_price"]
BASES = {
    "percent": "A percentage of the food",
    "dish_price": "Each dish's partner price (PPP)",
}
DISCOUNTS = {
    "company": "Out of Shero's share only",
    "shared": "Out of both shares, in proportion",
}
PAYABLE = {
    "delivered": "Once the order is delivered",
    "paid": "As soon as the customer pays",
}
# Each order's place in paying its kitchen.
STATUSES = {
    "upcoming": "Not delivered yet",
    "due": "Due to the kitchen",
    "paid_out": "Paid to the kitchen",
    "none": "Nothing owed (refunded)",
    "recover": "Refunded after payout",
}
# Orders that count: the customer paid, even if it was refunded later.
COUNTED = (PaymentStatus.PAID, PaymentStatus.REFUNDED)


class PayoutError(ValueError):
    """Something on the page that cannot be done; the message says why."""


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def _dec(value, default: Decimal = ZERO) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - a malformed snapshot value counts as none
        return default


def _percent(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("Use a number from 0 to 100")
    if value != value.quantize(CENT):
        raise ValueError("Use at most 2 decimal places for a percentage")
    return value.quantize(CENT)


# --- Rules --------------------------------------------------------------------
class KitchenRule(BaseModel):
    """One kitchen's own split. Blank fields follow the default rule."""

    model_config = ConfigDict(extra="forbid")
    outlet_id: uuid.UUID
    basis: Basis | None = None
    percent: Decimal | None = Field(None, ge=0, le=100)

    @field_validator("percent")
    @classmethod
    def two_places(cls, value):
        return None if value is None else _percent(value)


class Rules(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)
    basis: Basis = Field("percent", description="How the kitchen's share is worked out")
    kitchen_percent: Decimal = Field(
        Decimal("65"), ge=0, le=100,
        description="Kitchen's percentage of the food (also used for a dish with no PPP)")
    discounts: Literal["company", "shared"] = Field(
        "company", description="Coupons and loyalty points come")
    payable_after: Literal["delivered", "paid"] = Field(
        "delivered", description="An order is due to its kitchen")
    kitchens: list[KitchenRule] = Field(default_factory=list)

    @field_validator("kitchen_percent")
    @classmethod
    def two_places(cls, value):
        return _percent(value)

    @model_validator(mode="after")
    def one_rule_each(self):
        if len(self.kitchens) > 500:
            raise ValueError("Too many kitchen rules")
        if len({k.outlet_id for k in self.kitchens}) != len(self.kitchens):
            raise ValueError("Give each kitchen one rule")
        # A row that changes nothing is not kept.
        self.kitchens = [k for k in self.kitchens if k.basis is not None or k.percent is not None]
        return self

    def rule_for(self, outlet_id) -> tuple[str, Decimal]:
        """(basis, percent) for this kitchen: its own rule, else the default."""
        for rule in self.kitchens:
            if rule.outlet_id == outlet_id:
                return (rule.basis or self.basis,
                        self.kitchen_percent if rule.percent is None else rule.percent)
        return self.basis, self.kitchen_percent

    def has_own_rule(self, outlet_id) -> bool:
        return any(rule.outlet_id == outlet_id for rule in self.kitchens)


def describe(basis: str, percent) -> str:
    """The rule in a few words, for tables and statements."""
    pct = f"{_dec(percent).normalize():f}%"
    if basis == "dish_price":
        return f"Partner price (PPP); {pct} for a dish without one"
    return f"{pct} of the food"


async def rules(session) -> Rules:
    row = (await session.execute(select(AppSetting).where(AppSetting.key == KEY))).scalar_one_or_none()
    return Rules.model_validate_json(row.value) if row and row.value else Rules()


async def save_rules(session, data: dict, actor: str) -> Rules:
    saved = Rules.model_validate(data)
    ids = {rule.outlet_id for rule in saved.kitchens}
    if ids:
        found = set((await session.execute(select(Outlet.id).where(Outlet.id.in_(ids)))).scalars())
        if found != ids:
            raise PayoutError("One or more kitchens no longer exist; reload the page")
    row = (await session.execute(select(AppSetting).where(AppSetting.key == KEY))).scalar_one_or_none()
    if row is None:
        row = AppSetting(key=KEY, is_secret=False)
        session.add(row)
    row.value, row.updated_by = saved.model_dump_json(), actor
    await session.flush()
    return saved


# --- One order's split ----------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_locked(order: Order) -> bool:
    """Has the rule been copied onto this order (at payment, or later)?"""
    return bool(order.payout_split) and "gross" in order.payout_split


def snapshot(order: Order, rules_: Rules, ppp: dict[str, Decimal | None],
             coupon_off: str | None, *, by: str) -> dict:
    """The rule in force and each dish's share, to be kept on the order."""
    basis, percent = rules_.rule_for(order.outlet_id)
    lines, gross = [], ZERO
    for item in order.items or []:
        quantity = int(item.get("quantity") or 0)
        line_total = _dec(item.get("line_total"), None)
        if line_total is None:
            line_total = money(_dec(item.get("unit_price")) * quantity)
        rid = str(item.get("retailer_id") or "")
        unit = ppp.get(rid) if basis == "dish_price" else None
        share = money(unit * quantity) if unit is not None else money(line_total * percent / HUNDRED)
        lines.append({"retailer_id": rid, "name": item.get("name") or rid, "quantity": quantity,
                      "line_total": str(money(line_total)),
                      "ppp": None if unit is None else str(money(unit)), "share": str(share)})
        gross += share
    if not lines and order.dish_total:
        gross = money(order.dish_total * percent / HUNDRED)
    return {"basis": basis, "percent": str(percent), "discounts": rules_.discounts,
            "coupon_off": coupon_off, "lines": lines, "gross": str(money(gross)),
            "locked_at": _now(), "locked_by": by}


async def _menu_ppp(session, retailer_ids: Iterable[str]) -> dict[str, Decimal | None]:
    ids = sorted({rid for rid in retailer_ids if rid})
    if not ids:
        return {}
    rows = await session.execute(select(MenuItem.retailer_id, MenuItem.cost_price)
                                 .where(MenuItem.retailer_id.in_(ids)))
    return {rid: (None if cost is None else Decimal(cost)) for rid, cost in rows.all()}


def _coupon(order: Order) -> Decimal:
    return max(ZERO, _dec(order.discount) - _dec(order.loyalty_discount))


def _coupon_off(order: Order, promotion: Promotion | None) -> str | None:
    """Did the coupon come off the dishes or the delivery charge?"""
    if _coupon(order) <= 0:
        return None
    if promotion is not None:
        from app.services import promotions
        return "dishes" if promotions.comes_off_dishes(promotion) else "delivery"
    # The promotion has since been deleted: a free-delivery code took exactly
    # the delivery charge off.
    fee = _dec(order.delivery_fee)
    return "delivery" if fee > 0 and _coupon(order) == fee else "dishes"


async def _snapshots(session, orders: list[Order], rules_: Rules, *, by: str) -> dict:
    """Fresh snapshots for these orders, with one menu and promotion lookup."""
    if not orders:
        return {}
    ppp = await _menu_ppp(session, (str(item.get("retailer_id") or "")
                                    for order in orders for item in order.items or []))
    promo_ids = {order.promotion_id for order in orders if order.promotion_id}
    promos = {}
    if promo_ids:
        promos = {p.id: p for p in (await session.execute(
            select(Promotion).where(Promotion.id.in_(promo_ids)))).scalars()}
    return {order.id: snapshot(order, rules_, ppp, _coupon_off(order, promos.get(order.promotion_id)),
                               by=by) for order in orders}


def _keep(order: Order, fresh: dict) -> dict:
    """A new snapshot that keeps a hand-set amount and a payout already made."""
    old = order.payout_split or {}
    return {**fresh, **{key: old[key] for key in ("manual", "paid") if key in old}}


async def lock(session, order: Order, *, by: str = "payment") -> dict:
    """Copy today's rule onto the order (at payment)."""
    fresh = (await _snapshots(session, [order], await rules(session), by=by))[order.id]
    order.payout_split = _keep(order, fresh)
    await session.flush()
    return order.payout_split


@dataclass
class Split:
    order: Order
    outlet_id: uuid.UUID | None
    basis: str
    percent: Decimal
    discounts_rule: str
    food: Decimal
    discounts: Decimal          # coupon + loyalty points
    passed_on: Decimal          # delivery, Uber fees and tax
    paid: Decimal               # what the customer paid
    refunded: Decimal
    kitchen: Decimal            # owed to the kitchen now (or paid, once paid out)
    company: Decimal            # Shero's share
    status: str
    locked: bool
    manual: dict | None = None
    paid_out: Decimal | None = None
    lines: list = field(default_factory=list)

    @property
    def rule(self) -> str:
        return describe(self.basis, self.percent)

    @property
    def status_label(self) -> str:
        return STATUSES[self.status]

    @property
    def kitchen_percent_of_food(self) -> Decimal | None:
        if not self.food:
            return None
        return (self.kitchen / self.food * HUNDRED).quantize(Decimal("0.1"))


def _cancelled(order: Order) -> bool:
    return (order.payment_status == PaymentStatus.REFUNDED
            or order.stage in (OrderStage.CANCELLED, OrderStage.REFUNDED))


def settle(order: Order, snap: dict, payable_after: str, *, locked: bool = True) -> Split:
    """Every amount for one order, from its snapshot and its state now."""
    food = _dec(order.dish_total)
    coupon = _coupon(order)
    points = _dec(order.loyalty_discount)
    off_food = points + (coupon if snap.get("coupon_off") == "dishes" else ZERO)
    gross = _dec(snap.get("gross"))
    if snap.get("discounts") == "shared" and food > 0 and off_food > 0:
        owed = money(gross * max(ZERO, food - off_food) / food)
    else:
        owed = money(gross)

    passed_on = _dec(order.delivery_fee) + _dec(order.extra_fees) + _dec(order.tax)
    refunded = _dec(order.refund_amount)
    cancelled = _cancelled(order)
    if cancelled:
        owed = ZERO
        # The courier is still paid for a trip that was booked.
        passed_on = (_dec(order.delivery_fee) + _dec(order.extra_fees)) if order.uber_delivery_id else ZERO
    manual = snap.get("manual") or None
    if manual:
        owed = money(_dec(manual.get("amount")))

    paid_out = money(_dec(snap["paid"])) if order.payout_id and "paid" in snap else None
    kitchen = paid_out if paid_out is not None else owed
    if paid_out is not None:
        status = "recover" if cancelled and owed < paid_out else "paid_out"
    elif cancelled and not manual:
        status = "none"
    elif payable_after == "paid" or order.stage == OrderStage.DELIVERED or cancelled:
        status = "due"
    else:
        status = "upcoming"
    paid = _dec(order.total)
    return Split(
        order=order, outlet_id=order.outlet_id, basis=snap.get("basis") or "percent",
        percent=_dec(snap.get("percent")), discounts_rule=snap.get("discounts") or "company",
        food=money(food), discounts=money(coupon + points), passed_on=money(passed_on),
        paid=money(paid), refunded=money(refunded), kitchen=money(kitchen),
        company=money(paid - refunded - kitchen - passed_on), status=status, locked=locked,
        manual=manual, paid_out=paid_out, lines=list(snap.get("lines") or []))


async def splits(session, orders: list[Order], rules_: Rules | None = None) -> list[Split]:
    """Each order's split; an order paid before its rule was copied is worked
    out with today's rule."""
    rules_ = rules_ or await rules(session)
    loose = [order for order in orders if not is_locked(order)]
    fresh = await _snapshots(session, loose, rules_, by="today's rule")
    return [settle(order, order.payout_split if is_locked(order) else _keep(order, fresh[order.id]),
                   rules_.payable_after, locked=is_locked(order)) for order in orders]


# --- The team's actions --------------------------------------------------------
async def set_amount(session, order: Order, amount: str | None, note: str, actor: str) -> Split:
    """Set the kitchen's amount for one order by hand, or (blank) clear it."""
    if order.payout_id:
        raise PayoutError(f"{order.order_number} is already paid out; undo that payout first")
    if order.payment_status not in COUNTED:
        raise PayoutError(f"{order.order_number} has not been paid")
    if not is_locked(order):
        await lock(session, order, by=actor)
    split = dict(order.payout_split)
    text = (amount or "").strip().lstrip("$")
    if not text:
        split.pop("manual", None)
    else:
        value = _dec(text, None)
        if value is None or not value.is_finite() or value < 0:
            raise PayoutError("Enter the kitchen's amount in dollars, e.g. 42.50")
        if money(value) > money(order.total):
            raise PayoutError(f"The kitchen's amount cannot be more than the ${order.total:.2f} "
                              "the customer paid")
        split["manual"] = {"amount": str(money(value)), "note": (note or "").strip()[:NOTE_MAX],
                           "by": actor, "at": _now()}
    order.payout_split = split
    await session.flush()
    return settle(order, split, (await rules(session)).payable_after)


async def apply_rules(session, orders: list[Order], actor: str) -> int:
    """Copy today's rule onto orders not paid out yet. Hand-set amounts stay."""
    todo = [order for order in orders if not order.payout_id and order.payment_status in COUNTED]
    fresh = await _snapshots(session, todo, await rules(session), by=actor)
    for order in todo:
        order.payout_split = _keep(order, fresh[order.id])
    await session.flush()
    return len(todo)


async def pay(session, outlet: Outlet, due: list[Split], *, paid_on: date, reference: str,
              note: str, actor: str) -> PartnerPayout:
    """Record a payment to a kitchen for these due orders."""
    due = [s for s in due if s.status == "due" and s.outlet_id == outlet.id]
    if not due:
        raise PayoutError(f"Nothing is due to {outlet.name} for these dates")
    stamps = [s.order.paid_at for s in due if s.order.paid_at]
    payout = PartnerPayout(
        outlet_id=outlet.id, outlet_name=outlet.name,
        amount=money(sum((s.kitchen for s in due), ZERO)), order_count=len(due),
        first_paid_at=min(stamps) if stamps else None, last_paid_at=max(stamps) if stamps else None,
        paid_on=paid_on, reference=(reference or "").strip()[:REFERENCE_MAX] or None,
        note=(note or "").strip()[:NOTE_MAX] or None, created_by=actor)
    session.add(payout)
    await session.flush()
    loose = [s.order for s in due if not s.locked]
    fresh = await _snapshots(session, loose, await rules(session), by=actor)
    for s in due:
        base = s.order.payout_split if s.locked else _keep(s.order, fresh[s.order.id])
        s.order.payout_split = {**base, "paid": str(s.kitchen)}
        s.order.payout_id = payout.id
    await session.flush()
    return payout


async def undo(session, payout: PartnerPayout, actor: str) -> int:
    """Undo a payout recorded by mistake: its orders are due again."""
    if payout.voided_at:
        raise PayoutError("That payout was already undone")
    orders = list((await session.execute(select(Order).where(Order.payout_id == payout.id))).scalars())
    for order in orders:
        split = dict(order.payout_split or {})
        split.pop("paid", None)
        order.payout_split = split
        order.payout_id = None
    payout.voided_at = datetime.now(timezone.utc)
    payout.voided_by = actor
    await session.flush()
    return len(orders)


# --- Reports -------------------------------------------------------------------
@dataclass
class Totals:
    orders: int = 0
    food: Decimal = ZERO
    discounts: Decimal = ZERO
    passed_on: Decimal = ZERO
    paid: Decimal = ZERO
    refunded: Decimal = ZERO
    kitchen: Decimal = ZERO
    company: Decimal = ZERO
    due: Decimal = ZERO
    due_orders: int = 0
    upcoming: Decimal = ZERO
    paid_out: Decimal = ZERO
    recover: Decimal = ZERO

    def add(self, s: Split) -> None:
        self.orders += 1
        self.food += s.food
        self.discounts += s.discounts
        self.passed_on += s.passed_on
        self.paid += s.paid
        self.refunded += s.refunded
        self.kitchen += s.kitchen
        self.company += s.company
        if s.status == "due":
            self.due += s.kitchen
            self.due_orders += 1
        elif s.status == "upcoming":
            self.upcoming += s.kitchen
        elif s.status in ("paid_out", "recover"):
            self.paid_out += s.paid_out or ZERO
            if s.status == "recover":
                self.recover += s.paid_out or ZERO


def totals(items: Iterable[Split]) -> Totals:
    result = Totals()
    for s in items:
        result.add(s)
    return result


def by_kitchen(items: Iterable[Split]) -> dict:
    """{outlet_id: Totals}."""
    grouped: dict = {}
    for s in items:
        grouped.setdefault(s.outlet_id, Totals()).add(s)
    return grouped


async def orders_between(session, start: datetime | None, end: datetime | None,
                         outlet_id=None) -> list[Order]:
    """Orders the customer paid for, by payment time [start, end)."""
    query = select(Order).where(Order.payment_status.in_([str(s) for s in COUNTED]),
                                Order.paid_at.is_not(None))
    if start is not None:
        query = query.where(Order.paid_at >= start)
    if end is not None:
        query = query.where(Order.paid_at < end)
    if outlet_id is not None:
        query = query.where(Order.outlet_id == outlet_id)
    return list((await session.execute(query.order_by(Order.paid_at.desc()))).scalars())


async def ppp_coverage(session) -> dict:
    """How many dishes have a partner price, and what share of the menu
    price it is on average."""
    rows = (await session.execute(select(MenuItem.price, MenuItem.cost_price))).all()
    priced = [(Decimal(p), Decimal(c)) for p, c in rows if c is not None and p]
    share = (sum(c / p for p, c in priced) / len(priced) * HUNDRED).quantize(Decimal("0.1")) if priced else None
    return {"dishes": len(rows), "with_ppp": len(priced), "average_percent": share}


STATEMENT_COLUMNS = ("Order", "Paid at (UTC)", "Kitchen", "Stage", "Dishes", "Food",
                     "Coupon and points", "Delivery, fees and tax", "Customer paid", "Refunded",
                     "Kitchen share", "Shero share", "Rule", "Set by hand", "Status", "Payout")


def statement_csv(items: list[Split], kitchen_names: dict, payouts_: dict | None = None) -> str:
    """The orders as a spreadsheet, one row each, with a total row."""
    out = io.StringIO()
    out.write("﻿")                       # Excel reads the $ and names as UTF-8
    writer = csv.writer(out)
    writer.writerow(STATEMENT_COLUMNS)
    for s in items:
        o = s.order
        payout = (payouts_ or {}).get(o.payout_id)
        writer.writerow([
            o.order_number, o.paid_at.strftime("%Y-%m-%d %H:%M") if o.paid_at else "",
            kitchen_names.get(s.outlet_id, ""), o.stage,
            "; ".join(f"{line.get('quantity')} x {line.get('name')}" for line in s.lines),
            s.food, s.discounts, s.passed_on, s.paid, s.refunded, s.kitchen, s.company,
            s.rule, (s.manual or {}).get("note") or ("yes" if s.manual else ""), s.status_label,
            (f"{payout.paid_on} {payout.reference or ''}".strip() if payout else "")])
    t = totals(items)
    writer.writerow(["Total", "", "", "", f"{t.orders} orders", t.food, t.discounts, t.passed_on,
                     t.paid, t.refunded, t.kitchen, t.company, "", "", "", ""])
    return out.getvalue()
