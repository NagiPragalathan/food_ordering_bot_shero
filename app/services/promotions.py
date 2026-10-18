"""Coupon codes: the rules a code has to pass, and the discount it gives.

A promotion is created on the admin Offers page. A customer applies its code
on the ordering page (or picks it from the offers listed there); the code is
kept on their conversation, like the cart, so it survives closing the page
and is used again when the order is confirmed.

Two kinds of check, because they fail differently:

  * `check` - the code itself: switched on, inside its dates, not used up,
    allowed for this customer and this kitchen. A failure here means the code
    cannot be used at all, so it is dropped.
  * `amount_problem` - this cart: below the minimum order, or nothing to
    take off. The code stays applied and starts working once the cart
    qualifies ("Add $4.50 more to use SHERO10").

`discount_for` is the money. A percentage or flat discount comes off the
dishes (and tax is charged on what is left); free delivery takes off the
delivery charge. pricing.price_cart applies it; the order keeps the code,
title and amount, which is what the bill, Stripe and Zoho show.

A use is counted from orders, never from a counter that could drift: a paid
order, plus an unpaid one whose payment link is still open, so the last use
of a limited code cannot be handed to two people at once. A refunded order
gives its use back.
"""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import (Conversation, Customer, DiscountType, Order, OrderStage,
                           Outlet, PaymentStatus, Placement, Promotion)
from app.services import kitchen as kitchen_service
from app.services import slots

log = get_logger(__name__)

CENTS = Decimal("0.01")
ZERO = Decimal("0.00")
# Stripe will not take a card payment below 50 cents, so a discount never
# brings an order under it.
MIN_CHARGE = Decimal("0.50")
# Where the applied code is kept on the conversation.
CONTEXT_KEY = "coupon_code"

CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,31}$")
# Generated codes avoid characters that are misread: 0/O, 1/I/L.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"

# What the admin list calls a promotion right now.
LIVE, SCHEDULED, EXPIRED, PAUSED, USED_UP = "live", "scheduled", "expired", "paused", "used_up"
STATUS_LABELS = {LIVE: "Live", SCHEDULED: "Scheduled", EXPIRED: "Expired",
                 PAUSED: "Paused", USED_UP: "Limit reached"}


class PromoError(ValueError):
    """A code that cannot be used, worded for the customer."""


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP)


def normalize_code(raw) -> str:
    """What a customer typed, as it is stored: no spaces, upper case."""
    return re.sub(r"\s+", "", str(raw or "")).upper()[:32]


def generate_code(prefix: str = "SHERO", length: int = 5) -> str:
    return prefix + "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


# --- dates -------------------------------------------------------------------
async def local_today(session: AsyncSession, outlet: Outlet | None = None) -> date:
    """Today where the food is cooked: the kitchen's own day, not UTC's.

    An offer that ends on the 9th should still work at 9pm on the 9th in New
    Jersey, which is already the 10th in UTC.
    """
    outlet = outlet or await kitchen_service.get_kitchen(session)
    zone = slots.outlet_tz(outlet) if outlet is not None else timezone.utc
    return datetime.now(zone).date()


def short_date(day: date) -> str:
    """"Sep 9" - written out so no platform-specific strftime flag is needed."""
    return f"{day:%b} {day.day}"


# --- usage -------------------------------------------------------------------
@dataclass
class Usage:
    paid: int = 0                 # redeemed on a paid order
    held: int = 0                 # on an unpaid order with a live payment link
    discount_given: Decimal = ZERO
    revenue: Decimal = ZERO       # what those paid orders were charged

    @property
    def used(self) -> int:
        return self.paid + self.held


def _held(now: datetime):
    return and_(Order.payment_status == PaymentStatus.LINK_SENT,
                Order.stage == OrderStage.PENDING_PAYMENT,
                Order.payment_link_expires_at > now)


def _counted(now: datetime):
    """An order that takes one use of its code."""
    return or_(Order.payment_status == PaymentStatus.PAID, _held(now))


async def usage(session: AsyncSession, promotion_ids: list[uuid.UUID]) -> dict[uuid.UUID, Usage]:
    """Uses per promotion, in one query.

    SUM(CASE ...) rather than an aggregate FILTER clause, which MySQL (the
    AWS server's database) does not have.
    """
    if not promotion_ids:
        return {}
    now = datetime.now(timezone.utc)
    paid = Order.payment_status == PaymentStatus.PAID

    def total_where(condition, value=1):
        return func.coalesce(func.sum(case((condition, value), else_=0)), 0)

    rows = await session.execute(
        select(
            Order.promotion_id,
            total_where(paid),
            total_where(_held(now)),
            total_where(paid, Order.discount),
            total_where(paid, Order.total),
        )
        .where(Order.promotion_id.in_(promotion_ids), _counted(now))
        .group_by(Order.promotion_id)
    )
    found = {pid: Usage(paid=int(p or 0), held=int(h or 0),
                        discount_given=money(d or 0), revenue=money(r or 0))
             for pid, p, h, d, r in rows}
    return {pid: found.get(pid, Usage()) for pid in promotion_ids}


async def _uses_by_others(session: AsyncSession, promotion: Promotion,
                          customer: Customer) -> int:
    """Uses that count against the overall limit for this customer.

    The customer's own unpaid orders are left out: confirming a new order
    with the code replaces them (order_changes.release_other_promo_holds),
    so they must not block it.
    """
    now = datetime.now(timezone.utc)
    return int(await session.scalar(
        select(func.count(Order.id)).where(
            Order.promotion_id == promotion.id,
            or_(Order.payment_status == PaymentStatus.PAID,
                and_(_held(now), Order.customer_id != customer.id)),
        )
    ) or 0)


async def _paid_uses_by(session: AsyncSession, promotion: Promotion,
                        customer: Customer) -> int:
    return int(await session.scalar(
        select(func.count(Order.id)).where(
            Order.promotion_id == promotion.id,
            Order.customer_id == customer.id,
            Order.payment_status == PaymentStatus.PAID,
        )
    ) or 0)


async def has_paid_order(session: AsyncSession, customer: Customer) -> bool:
    return bool(await session.scalar(
        select(func.count(Order.id)).where(
            Order.customer_id == customer.id,
            Order.payment_status == PaymentStatus.PAID,
        )
    ))


# --- status and rules --------------------------------------------------------
def status(promotion: Promotion, *, today: date, used: int = 0) -> str:
    """Live / Scheduled / Expired / Paused / Limit reached, for the admin."""
    if not promotion.is_active:
        return PAUSED
    if promotion.ends_on and today > promotion.ends_on:
        return EXPIRED
    if promotion.starts_on and today < promotion.starts_on:
        return SCHEDULED
    if promotion.usage_limit is not None and used >= promotion.usage_limit:
        return USED_UP
    return LIVE


async def find_by_code(session: AsyncSession, code: str, *,
                       lock: bool = False) -> Promotion | None:
    """The promotion with this code. `lock` holds its row until commit, so two
    customers confirming at once cannot both take a limited code's last use."""
    cleaned = normalize_code(code)
    if not cleaned:
        return None
    query = select(Promotion).where(Promotion.code == cleaned)
    if lock:
        query = query.with_for_update()
    return (await session.execute(query)).scalar_one_or_none()


async def check(session: AsyncSession, promotion: Promotion, *, customer: Customer,
                outlet: Outlet | None, today: date | None = None) -> None:
    """Raise PromoError if this customer cannot use the code at all."""
    today = today or await local_today(session, outlet)

    if not promotion.is_active:
        raise PromoError(f"{promotion.code} is not active right now.")
    if promotion.starts_on and today < promotion.starts_on:
        raise PromoError(f"{promotion.code} starts on {short_date(promotion.starts_on)}.")
    if promotion.ends_on and today > promotion.ends_on:
        raise PromoError(f"{promotion.code} has expired.")
    if (promotion.outlet_id is not None and outlet is not None
            and promotion.outlet_id != outlet.id):
        raise PromoError(f"{promotion.code} is not valid for the kitchen that "
                         "delivers to your address.")

    if promotion.usage_limit is not None:
        if await _uses_by_others(session, promotion, customer) >= promotion.usage_limit:
            raise PromoError(f"{promotion.code} has been fully claimed. "
                             "Keep an eye out for the next offer!")

    if promotion.first_order_only and await has_paid_order(session, customer):
        raise PromoError(f"{promotion.code} is for your first order only.")

    if promotion.per_customer_limit is not None:
        used = await _paid_uses_by(session, promotion, customer)
        if used >= promotion.per_customer_limit:
            raise PromoError(
                f"You have already used {promotion.code}." if promotion.per_customer_limit == 1
                else f"You have used {promotion.code} the maximum "
                     f"{promotion.per_customer_limit} times.")


async def resolve(session: AsyncSession, code: str, *, customer: Customer,
                  outlet: Outlet | None, lock: bool = False) -> Promotion:
    """The promotion behind a typed code, checked; PromoError if unusable."""
    cleaned = normalize_code(code)
    if not cleaned:
        raise PromoError("Enter a coupon code.")
    promotion = await find_by_code(session, cleaned, lock=lock)
    if promotion is None:
        raise PromoError(f"{cleaned} is not a valid code. Check it and try again.")
    await check(session, promotion, customer=customer, outlet=outlet)
    return promotion


# --- the money ---------------------------------------------------------------
def discount_for(promotion: Promotion, *, dish_total: Decimal,
                 delivery_fee: Decimal) -> Decimal:
    """What the code takes off this order (before the minimum-charge guard)."""
    dish_total = money(dish_total)
    value = money(promotion.discount_value or 0)
    match promotion.discount_type:
        case DiscountType.PERCENT:
            amount = money(dish_total * value / Decimal("100"))
            if promotion.max_discount is not None:
                amount = min(amount, money(promotion.max_discount))
        case DiscountType.FLAT:
            amount = value
        case DiscountType.FREE_DELIVERY:
            return max(ZERO, money(delivery_fee))
        case _:
            return ZERO
    # Never more than the dishes: a discount is not a refund of delivery.
    return max(ZERO, min(amount, dish_total))


def comes_off_dishes(promotion: Promotion | None) -> bool:
    """Percent and flat come off the dishes, so tax is on what is left."""
    return promotion is not None and promotion.discount_type != DiscountType.FREE_DELIVERY


def amount_problem(promotion: Promotion, *, dish_total: Decimal,
                   delivery_fee: Decimal | None = None) -> str | None:
    """Why this cart does not get the discount yet, or None if it does.

    `delivery_fee` is None before delivery has been priced (the cart and the
    menu page), when free delivery cannot be judged yet.
    """
    minimum = money(promotion.min_order or 0)
    if money(dish_total) < minimum:
        short = minimum - money(dish_total)
        return (f"Add ${short:.2f} more to use {promotion.code} "
                f"(minimum order ${minimum:.2f}).")
    if promotion.discount_type == DiscountType.FREE_DELIVERY:
        if delivery_fee is not None and money(delivery_fee) <= 0:
            return "Delivery is already free on this order."
        return None
    if delivery_fee is not None and discount_for(
            promotion, dish_total=dish_total, delivery_fee=delivery_fee) <= 0:
        return f"{promotion.code} takes nothing off this order."
    return None


# --- describing an offer -----------------------------------------------------
def headline(promotion: Promotion) -> str:
    """"20% off, up to $10" / "$5 off" / "Free delivery"."""
    value = money(promotion.discount_value or 0)
    match promotion.discount_type:
        case DiscountType.PERCENT:
            text = f"{plain(value)}% off"
            if promotion.max_discount is not None:
                text += f", up to ${plain(money(promotion.max_discount))}"
            return text
        case DiscountType.FLAT:
            return f"${plain(value)} off"
        case DiscountType.FREE_DELIVERY:
            return "Free delivery"
    return "Discount"


def default_tag(promotion: Promotion) -> str:
    """The banner label when the admin left it blank: "FLAT 20% OFF"."""
    value = money(promotion.discount_value or 0)
    match promotion.discount_type:
        case DiscountType.PERCENT:
            return f"FLAT {plain(value)}% OFF"
        case DiscountType.FLAT:
            return f"${plain(value)} OFF"
        case DiscountType.FREE_DELIVERY:
            return "FREE DELIVERY"
    return "OFFER"


def conditions(promotion: Promotion, *, with_end: bool = True) -> list[str]:
    """The small print, one short phrase each. `with_end` False leaves out
    the end date, for a view that shows the dates separately."""
    terms = []
    if promotion.min_order and money(promotion.min_order) > 0:
        terms.append(f"Min order ${plain(money(promotion.min_order))}")
    if promotion.first_order_only:
        terms.append("First order only")
    if promotion.per_customer_limit == 1:
        terms.append("Once per customer")
    elif promotion.per_customer_limit:
        terms.append(f"{promotion.per_customer_limit} uses per customer")
    if with_end and promotion.ends_on:
        terms.append(f"Valid till {short_date(promotion.ends_on)}")
    return terms


def plain(value: Decimal) -> str:
    """10.00 -> "10", 12.50 -> "12.50"."""
    return f"{value:.0f}" if value == value.to_integral_value() else f"{value:.2f}"


def public(promotion: Promotion, *, dish_total: Decimal | None = None) -> dict:
    """An offer as the ordering page shows it."""
    problem = (amount_problem(promotion, dish_total=dish_total)
               if dish_total is not None else None)
    saving = None
    if dish_total is not None and problem is None and comes_off_dishes(promotion):
        saving = float(discount_for(promotion, dish_total=dish_total, delivery_fee=ZERO))
    return {
        "code": promotion.code,
        "title": promotion.title,
        "tag": promotion.tag or default_tag(promotion),
        "headline": headline(promotion),
        "description": promotion.description or "",
        "terms": conditions(promotion),
        "min_order": float(promotion.min_order or 0),
        "type": promotion.discount_type,
        "eligible": problem is None,
        "hint": problem or "",
        "saving": saving,
    }


async def offers_for(session: AsyncSession, customer: Customer, *,
                     outlet: Outlet | None, dish_total: Decimal | None = None,
                     placements: tuple[str, ...] = (Placement.MENU, Placement.CHECKOUT),
                     ) -> list[dict]:
    """Offers this customer could use, in display order.

    Codes they cannot use at all (expired, used up, first order only...) are
    left out; ones the cart is not big enough for yet are listed with a hint.
    """
    promotions = list((await session.execute(
        select(Promotion)
        .where(Promotion.is_active.is_(True),
               Promotion.placement.in_([str(p) for p in placements]))
        .order_by(Promotion.display_order, Promotion.created_at)
    )).scalars())
    today = await local_today(session, outlet)
    offers = []
    for promotion in promotions:
        try:
            await check(session, promotion, customer=customer, outlet=outlet, today=today)
        except PromoError:
            continue
        offer = public(promotion, dish_total=dish_total)
        offer["placement"] = promotion.placement
        offers.append(offer)
    return offers


# --- the code kept on the conversation ---------------------------------------
def stored_code(conversation: Conversation) -> str | None:
    return normalize_code(conversation.get(CONTEXT_KEY)) or None


def remember(conversation: Conversation, code: str) -> None:
    conversation.set(**{CONTEXT_KEY: normalize_code(code)})


def forget(conversation: Conversation) -> None:
    conversation.clear(CONTEXT_KEY)


async def stored_promotion(session: AsyncSession, conversation: Conversation, *,
                           customer: Customer, outlet: Outlet | None,
                           lock: bool = False) -> tuple[Promotion | None, str | None]:
    """The customer's applied code, checked again now.

    Returns (promotion, None), or (None, why) when the code stopped working
    since it was applied - it is then dropped, so the customer is told once.
    """
    code = stored_code(conversation)
    if not code:
        return None, None
    try:
        return await resolve(session, code, customer=customer, outlet=outlet, lock=lock), None
    except PromoError as exc:
        forget(conversation)
        log.info("stored_coupon_dropped", code=code, customer_id=str(customer.id),
                 reason=str(exc))
        return None, f"{exc} It has been removed."
