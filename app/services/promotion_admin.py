"""Saving promotions from the admin Offers page.

The route stays thin: it hands the posted form here. Parsing is pure, so it
can be tested without a request; every message is worded for the admin and
says which field to fix.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DiscountType, Outlet, Placement, Promotion
from app.services import promotions

DISCOUNT_TYPES = [
    (DiscountType.PERCENT.value, "Percentage (%)"),
    (DiscountType.FLAT.value, "Flat amount ($)"),
    (DiscountType.FREE_DELIVERY.value, "Free delivery"),
]
PLACEMENTS = [
    (Placement.MENU.value, "Menu banner + checkout",
     "A banner on the menu page, and listed at checkout."),
    (Placement.CHECKOUT.value, "Checkout only",
     "Listed under Offers on the order summary."),
    (Placement.HIDDEN.value, "Hidden - code only",
     "Never listed. Works when typed: for codes you hand out yourself."),
]


class PromotionFormError(ValueError):
    """A problem with the posted form, worded for the admin."""


@dataclass
class PromotionForm:
    title: str
    code: str
    tag: str | None
    description: str | None
    discount_type: str
    discount_value: Decimal
    min_order: Decimal
    max_discount: Decimal | None
    usage_limit: int | None
    per_customer_limit: int | None
    first_order_only: bool
    outlet_id: uuid.UUID | None
    starts_on: date | None
    ends_on: date | None
    placement: str
    display_order: int
    is_active: bool


def defaults() -> dict:
    """What a new promotion's form starts with."""
    return {
        "title": "", "code": promotions.generate_code(), "tag": "", "description": "",
        "discount_type": DiscountType.PERCENT.value, "discount_value": "",
        "min_order": "0", "max_discount": "", "usage_limit": "", "per_customer_limit": "1",
        "first_order_only": False, "outlet_id": "", "starts_on": "", "ends_on": "",
        "placement": Placement.CHECKOUT.value, "display_order": "0", "is_active": True,
    }


def values_of(promotion: Promotion) -> dict:
    """A saved promotion in the shape the form reads."""
    def num(value) -> str:
        return "" if value is None else promotions.plain(promotions.money(value))
    return {
        "title": promotion.title, "code": promotion.code,
        "tag": promotion.tag or "", "description": promotion.description or "",
        "discount_type": promotion.discount_type,
        "discount_value": num(promotion.discount_value),
        "min_order": num(promotion.min_order), "max_discount": num(promotion.max_discount),
        "usage_limit": "" if promotion.usage_limit is None else str(promotion.usage_limit),
        "per_customer_limit": ("" if promotion.per_customer_limit is None
                               else str(promotion.per_customer_limit)),
        "first_order_only": promotion.first_order_only,
        "outlet_id": str(promotion.outlet_id) if promotion.outlet_id else "",
        "starts_on": promotion.starts_on.isoformat() if promotion.starts_on else "",
        "ends_on": promotion.ends_on.isoformat() if promotion.ends_on else "",
        "placement": promotion.placement, "display_order": str(promotion.display_order),
        "is_active": promotion.is_active,
    }


def posted_values(form) -> dict:
    """The form as posted, to show it again with an error and nothing lost."""
    values = {key: str(form.get(key) or "") for key in defaults()}
    values["first_order_only"] = bool(form.get("first_order_only"))
    values["is_active"] = bool(form.get("is_active"))
    return values


def parse_form(form) -> PromotionForm:
    """Read and check the posted promotion form."""
    def text(key: str) -> str:
        return str(form.get(key) or "").strip()

    title = text("title")
    if not title:
        raise PromotionFormError("Give the promotion a title.")
    code = promotions.normalize_code(text("code"))
    if not code:
        raise PromotionFormError("Enter a coupon code, or press Generate.")
    if not promotions.CODE_PATTERN.match(code):
        raise PromotionFormError(
            "A coupon code is 3 to 32 letters or digits (dashes and underscores "
            "allowed), starting with a letter or digit.")

    discount_type = text("discount_type") or DiscountType.PERCENT.value
    if discount_type not in {t.value for t in DiscountType}:
        raise PromotionFormError("Choose a discount type.")

    value = _money(text("discount_value"), "Discount value")
    max_discount = _money(text("max_discount"), "Max discount")
    if discount_type == DiscountType.PERCENT:
        if value is None or not (0 < value <= 100):
            raise PromotionFormError("A percentage discount is between 1 and 100.")
        if max_discount is not None and max_discount <= 0:
            raise PromotionFormError("Max discount must be more than $0, or blank for no cap.")
    elif discount_type == DiscountType.FLAT:
        if value is None or value <= 0:
            raise PromotionFormError("Enter how many dollars the code takes off.")
        max_discount = None          # a flat amount is its own cap
    else:
        value, max_discount = Decimal("0"), None

    min_order = _money(text("min_order"), "Min order") or Decimal("0")
    if min_order < 0:
        raise PromotionFormError("Min order cannot be negative.")

    starts_on, ends_on = _date(text("starts_on"), "Start date"), _date(text("ends_on"), "End date")
    if starts_on and ends_on and ends_on < starts_on:
        raise PromotionFormError("The end date is before the start date.")

    placement = text("placement") or Placement.CHECKOUT.value
    if placement not in {p.value for p in Placement}:
        raise PromotionFormError("Choose where the offer is shown.")

    outlet_id = None
    if text("outlet_id"):
        try:
            outlet_id = uuid.UUID(text("outlet_id"))
        except ValueError as exc:
            raise PromotionFormError("Choose a kitchen from the list.") from exc

    return PromotionForm(
        title=title[:120],
        code=code,
        tag=text("tag")[:40] or None,
        description=text("description")[:255] or None,
        discount_type=discount_type,
        discount_value=value,
        min_order=min_order,
        max_discount=max_discount,
        usage_limit=_count(text("usage_limit"), "Usage limit"),
        per_customer_limit=_count(text("per_customer_limit"), "Uses per customer"),
        first_order_only=bool(form.get("first_order_only")),
        outlet_id=outlet_id,
        starts_on=starts_on,
        ends_on=ends_on,
        placement=placement,
        display_order=_int(text("display_order"), "Display order") or 0,
        is_active=bool(form.get("is_active")),
    )


async def save(session: AsyncSession, promotion: Promotion | None, data: PromotionForm,
               *, by: str | None = None) -> Promotion:
    """Create or update. Codes are unique, whatever their case."""
    clash = (await session.execute(
        select(Promotion.id).where(Promotion.code == data.code)
    )).scalar_one_or_none()
    if clash is not None and (promotion is None or clash != promotion.id):
        raise PromotionFormError(f"Another promotion already uses the code {data.code}.")
    if data.outlet_id and await session.get(Outlet, data.outlet_id) is None:
        raise PromotionFormError("That kitchen no longer exists. Choose another.")

    if promotion is None:
        promotion = Promotion(created_by=by)
        session.add(promotion)
    for key, value in asdict(data).items():
        setattr(promotion, key, value)
    await session.flush()
    return promotion


async def duplicate(session: AsyncSession, source: Promotion, *, by: str | None = None) -> Promotion:
    """A paused copy with a fresh code, to edit into the next offer."""
    data = PromotionForm(**{key: getattr(source, key) for key in PromotionForm.__annotations__})
    data.title = f"{source.title} (copy)"[:120]
    data.is_active = False
    for _ in range(10):
        data.code = promotions.generate_code(prefix=source.code[:20])
        try:
            return await save(session, None, data, by=by)
        except PromotionFormError:
            continue
    raise PromotionFormError("Could not find a free code for the copy. Try again.")


# --- field readers ------------------------------------------------------------
def _money(raw: str, label: str) -> Decimal | None:
    if not raw:
        return None
    try:
        value = Decimal(raw.replace("$", "").replace(",", "").replace("%", "").strip())
    except InvalidOperation as exc:
        raise PromotionFormError(f"{label} must be a number.") from exc
    if not value.is_finite():
        raise PromotionFormError(f"{label} must be a number.")
    return promotions.money(value)


def _int(raw: str, label: str) -> int | None:
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise PromotionFormError(f"{label} must be a whole number.") from exc


def _count(raw: str, label: str) -> int | None:
    """A limit: blank or 0 for unlimited, else at least 1."""
    value = _int(raw, label)
    if value is None or value == 0:
        return None
    if value < 0:
        raise PromotionFormError(f"{label} cannot be negative. Leave it blank for unlimited.")
    return value


def _date(raw: str, label: str) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise PromotionFormError(f"{label} is not a valid date.") from exc
