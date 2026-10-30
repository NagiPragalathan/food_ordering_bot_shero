"""Kitchen partner payouts: how each paid order's money splits between its
kitchen partner and Shero.

Set on the admin Payouts page: the kitchen partner's percentage, and Shero's
- the rest, so the two always make 100. One split for every kitchen, or,
with "Same split for every kitchen" off, a percentage for each kitchen (a
kitchen without one uses the shared percentage).

    The kitchen gets   its percentage of the food (the dishes).
    Shero gets         what the customer paid, less the kitchen's amount and
                       the delivery, Uber fees and tax that Shero passes on -
                       so a coupon or loyalty points come out of Shero's part.

    $100 of food, $8 delivery, kitchen 65%:
        the customer pays 108 = kitchen 65 + delivery 8 + Shero 35

A refunded or cancelled order gives the kitchen nothing. Every order is
worked out with today's percentages, so changing them changes the amounts
shown for every order.

(The first version also recorded payments to kitchens and copied the split
onto each order at payment - `partner_payouts`, `orders.payout_split`,
`orders.payout_id`. Those are no longer written or read.)
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select

from app.db.models import AppSetting, Order, OrderStage, Outlet, PaymentStatus

KEY = "PARTNER_SPLIT"
CENT = Decimal("0.01")
ZERO = Decimal("0.00")
HUNDRED = Decimal(100)
COUNTED = (PaymentStatus.PAID, PaymentStatus.REFUNDED)


class PayoutError(ValueError):
    """A split that cannot be saved; the message is shown to the admin."""


def money(value) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _dec(value, default: Decimal = ZERO) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - anything unreadable counts as the default
        return default


def _percent(value) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


# --- The split ---------------------------------------------------------------------------------
class SplitSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")     # the first version's options are ignored
    same_for_all: bool = True
    kitchen_percent: Decimal = Field(Decimal("65"), ge=0, le=100)
    # A kitchen's own percentage, used while same_for_all is off.
    kitchens: dict[uuid.UUID, Decimal] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _first_version(cls, data):
        """The first version kept kitchen rules as a list of
        {outlet_id, basis, percent}; a kitchen with its own percentage then
        meant the split was not the same for all."""
        if isinstance(data, dict) and isinstance(data.get("kitchens"), list):
            own = {str(r["outlet_id"]): r["percent"] for r in data["kitchens"]
                   if isinstance(r, dict) and r.get("outlet_id") and r.get("percent") is not None}
            data = {**data, "kitchens": own}
            data.setdefault("same_for_all", not own)
        return data

    @field_validator("kitchen_percent")
    @classmethod
    def _two_places(cls, value):
        return _percent(value)

    @field_validator("kitchens")
    @classmethod
    def _each_percent(cls, values):
        for value in values.values():
            if not ZERO <= Decimal(value) <= HUNDRED:
                raise ValueError("a kitchen's percentage is between 0 and 100")
        return {k: _percent(v) for k, v in values.items()}

    @property
    def shero_percent(self) -> Decimal:
        return HUNDRED - self.kitchen_percent

    def percent_for(self, outlet_id) -> Decimal:
        """The kitchen partner's percentage for this kitchen."""
        if not self.same_for_all and outlet_id in self.kitchens:
            return self.kitchens[outlet_id]
        return self.kitchen_percent


async def settings(session) -> SplitSettings:
    row = (await session.execute(select(AppSetting).where(AppSetting.key == KEY))).scalar_one_or_none()
    return SplitSettings.model_validate_json(row.value) if row and row.value else SplitSettings()


async def save(session, data: dict, actor: str) -> SplitSettings:
    try:
        saved = SplitSettings.model_validate(data)
    except ValueError as exc:
        raise PayoutError(_readable(exc)) from None
    if saved.kitchens:
        found = set((await session.execute(
            select(Outlet.id).where(Outlet.id.in_(saved.kitchens)))).scalars())
        if found != set(saved.kitchens):
            raise PayoutError("One or more kitchens no longer exist; reload the page")
    row = (await session.execute(select(AppSetting).where(AppSetting.key == KEY))).scalar_one_or_none()
    if row is None:
        row = AppSetting(key=KEY, is_secret=False)
        session.add(row)
    row.value, row.updated_by = saved.model_dump_json(), actor
    await session.flush()
    return saved


def _readable(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        return "; ".join(str(e.get("msg", "")).removeprefix("Value error, ") for e in errors())
    return str(exc)


# --- Each order ---------------------------------------------------------------------------------
@dataclass
class Split:
    order: Order
    outlet_id: uuid.UUID | None
    percent: Decimal            # the kitchen partner's percentage
    food: Decimal
    discounts: Decimal          # coupon + loyalty points
    passed_on: Decimal          # delivery, Uber fees and tax
    paid: Decimal               # what the customer paid
    refunded: Decimal
    kitchen: Decimal            # what the kitchen gets
    shero: Decimal              # what Shero gets
    cancelled: bool

    @property
    def shero_percent(self) -> Decimal:
        return HUNDRED - self.percent

    @property
    def rule(self) -> str:
        return f"Kitchen {self.percent.normalize():f}% · Shero {self.shero_percent.normalize():f}%"


def _cancelled(order: Order) -> bool:
    return (order.payment_status == PaymentStatus.REFUNDED
            or order.stage in (OrderStage.CANCELLED, OrderStage.REFUNDED))


def split(order: Order, percent: Decimal) -> Split:
    """Every amount for one order, with the kitchen partner's percentage."""
    food = _dec(order.dish_total)
    cancelled = _cancelled(order)
    passed_on = _dec(order.delivery_fee) + _dec(order.extra_fees) + _dec(order.tax)
    if cancelled:
        # The courier is still paid for a trip that was booked.
        passed_on = (_dec(order.delivery_fee) + _dec(order.extra_fees)) if order.uber_delivery_id else ZERO
    kitchen = ZERO if cancelled else money(food * percent / HUNDRED)
    paid, refunded = _dec(order.total), _dec(order.refund_amount)
    return Split(order=order, outlet_id=order.outlet_id, percent=percent, food=money(food),
                 discounts=money(_dec(order.discount)), passed_on=money(passed_on),
                 paid=money(paid), refunded=money(refunded), kitchen=money(kitchen),
                 shero=money(paid - refunded - kitchen - passed_on), cancelled=cancelled)


async def splits(session, orders: list[Order], settings_: SplitSettings | None = None) -> list[Split]:
    settings_ = settings_ or await settings(session)
    return [split(order, settings_.percent_for(order.outlet_id)) for order in orders]


async def orders_between(session, start: datetime | None, end: datetime | None,
                         outlet_id=None) -> list[Order]:
    """Orders the customer paid for, by payment time [start, end), newest first."""
    query = select(Order).where(Order.payment_status.in_([str(s) for s in COUNTED]),
                                Order.paid_at.is_not(None))
    if start is not None:
        query = query.where(Order.paid_at >= start)
    if end is not None:
        query = query.where(Order.paid_at < end)
    if outlet_id is not None:
        query = query.where(Order.outlet_id == outlet_id)
    return list((await session.execute(query.order_by(Order.paid_at.desc()))).scalars())


# --- Totals and the spreadsheet ---------------------------------------------------------------
@dataclass
class Totals:
    orders: int = 0
    food: Decimal = ZERO
    discounts: Decimal = ZERO
    passed_on: Decimal = ZERO
    paid: Decimal = ZERO
    refunded: Decimal = ZERO
    kitchen: Decimal = ZERO
    shero: Decimal = ZERO

    def add(self, s: Split) -> None:
        self.orders += 1
        self.food += s.food
        self.discounts += s.discounts
        self.passed_on += s.passed_on
        self.paid += s.paid
        self.refunded += s.refunded
        self.kitchen += s.kitchen
        self.shero += s.shero


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


CSV_COLUMNS = ("Order", "Paid at (UTC)", "Kitchen", "Status", "Dishes", "Customer paid", "Food",
               "Coupon and points", "Delivery, fees and tax", "Refunded", "Kitchen %",
               "Kitchen gets", "Shero %", "Shero gets")


def orders_csv(items: list[Split], kitchen_names: dict) -> str:
    """The orders as a spreadsheet, one row each, with a total row."""
    out = io.StringIO()
    out.write("﻿")                       # Excel reads the $ and names as UTF-8
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS)
    for s in items:
        o = s.order
        writer.writerow([
            o.order_number, o.paid_at.strftime("%Y-%m-%d %H:%M") if o.paid_at else "",
            kitchen_names.get(s.outlet_id, ""), o.stage,
            "; ".join(f"{i.get('quantity')} x {i.get('name')}" for i in o.items or []),
            s.paid, s.food, s.discounts, s.passed_on, s.refunded, s.percent, s.kitchen,
            s.shero_percent, s.shero])
    t = totals(items)
    writer.writerow(["Total", "", "", "", f"{t.orders} orders", t.paid, t.food, t.discounts,
                     t.passed_on, t.refunded, "", t.kitchen, "", t.shero])
    return out.getvalue()
