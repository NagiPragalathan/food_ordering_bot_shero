"""A campaign's words with the customer's details filled in."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.db.models import Outlet, Promotion
from app.services import cart as cart_service
from app.services import order_link, promotions
from app.services.nudges.audience import Candidate
from app.services.nudges.config import GlobalSettings

MAX_LINES = 4


class _Blank(dict):
    """Unknown or unavailable placeholders render as nothing, never as
    "{cart_items}" in front of a customer. (Saving a campaign already refuses
    names that do not exist.)"""

    def __missing__(self, key: str) -> str:
        return ""


def money(value) -> str:
    return f"${Decimal(str(value or 0)):.2f}"


def dishes(lines: list[dict]) -> str:
    """"2 × Sambar, 1 × Rasam and 3 more"."""
    parts = [f"{int(line.get('quantity') or 1)} × {line.get('name') or 'dish'}"
             for line in lines or []]
    if len(parts) > MAX_LINES:
        return ", ".join(parts[:MAX_LINES]) + f" and {len(parts) - MAX_LINES} more"
    return ", ".join(parts)


def short_day(moment: datetime | None) -> str:
    return f"{moment.day} {moment:%b}" if moment else ""


def values(candidate: Candidate, *, settings: GlobalSettings, promotion: Promotion | None,
           kitchen: Outlet | None, now: datetime) -> dict[str, str]:
    customer, facts = candidate.customer, candidate.facts
    name = customer.greeting_name
    found = {
        "name": name,
        "first_name": name.split()[0] if name else "there",
        "shop_name": settings.shop_name,
        "kitchen": kitchen.name if kitchen else "",
        "paid_orders": str(facts.paid_orders),
        "total_spent": money(facts.total_spent),
    }
    try:
        found["order_link"] = order_link.build_url(customer.id)
    except RuntimeError:
        found["order_link"] = ""

    if candidate.cart:
        found.update(cart_items=dishes(candidate.cart),
                     cart_total=money(cart_service.subtotal(candidate.cart)),
                     cart_count=str(cart_service.item_count(candidate.cart)))
    if candidate.order is not None:
        found.update(order_number=candidate.order.order_number,
                     order_total=money(candidate.order.total),
                     order_items=dishes(candidate.order.items))
    last = facts.last
    if last is not None:
        found.update(last_order_items=dishes(last.items),
                     last_order_date=short_day(last.paid_at),
                     days_since_last_order=str(max((now - last.paid_at).days, 0))
                     if last.paid_at else "")
    if promotion is not None:
        found.update(
            coupon_code=promotion.code,
            coupon_title=promotion.title,
            coupon_offer=promotions.headline(promotion),
            coupon_terms=" · ".join(promotions.conditions(promotion, with_end=False)),
            coupon_ends=(f"Valid till {promotions.short_date(promotion.ends_on)}."
                         if promotion.ends_on else ""),
        )
    return found


def fill(text: str, found: dict[str, str]) -> str:
    """The text with placeholders replaced; spaces left by empty ones tidied."""
    rendered = (text or "").format_map(_Blank(found))
    lines = [" ".join(line.split()) for line in rendered.splitlines()]
    return "\n".join(lines).strip()
