"""Admin loyalty policy, customer balances, history and audited adjustments."""
from __future__ import annotations
import uuid
from decimal import Decimal
from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.admin.deps import redirect, render, require_admin
from app.db.models import AdminUser, Customer, LoyaltyAccount, MenuItem, Outlet
from app.db.session import get_session
from app.services import loyalty

router = APIRouter(prefix="/loyalty", tags=["admin"])

# Plain number and switch fields, by section of the page; tiers, dishes,
# kitchens and promotion days have their own controls.
SECTIONS = [
    ("Program", "Switch the program and its two halves on or off.",
     ["enabled", "earning_enabled", "redemption_enabled", "name"]),
    ("Earning points", "What a paid order earns. Delivery, fees and tax never earn.",
     ["points_per_currency", "minimum_earn_subtotal", "max_earn_per_order", "earn_with_coupons",
      "earn_on_redemption"]),
    ("Bonus points", "Fixed points added on top of what the food earns. 0 turns a bonus off.",
     ["first_order_bonus", "points_per_order", "repeat_order_bonus", "repeat_from_order",
      "milestone_every", "milestone_bonus", "big_order_threshold", "big_order_bonus"]),
    ("Spending points", "What points are worth and how many an order can use.",
     ["point_value", "minimum_redeem_points", "redeem_step", "minimum_redeem_subtotal",
      "maximum_redeem_percent", "maximum_redeem_amount", "stack_with_coupons"]),
]
HINTS = {
    "points_per_currency": "e.g. 1 = 1 point per $1 of food",
    "max_earn_per_order": "0 = no cap",
    "repeat_from_order": "the first order number that gets the repeat bonus",
    "milestone_every": "e.g. 5 = every 5th order; 0 = off",
    "big_order_threshold": "eligible food in $; 0 = off",
    "point_value": "in $, e.g. 0.01 = 100 points for $1",
    "maximum_redeem_amount": "in $; 0 = no fixed cap",
    "maximum_redeem_percent": "% of the food points may cover",
}
DAY_LABELS = dict(zip(loyalty.DAYS, ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")))
# The examples table: a few order sizes and order numbers.
EXAMPLE_SPENDS = (Decimal(20), Decimal(50), Decimal(100))
EXAMPLE_ORDERS = (1, 2, 5, 10)


def _fields(rules) -> list[dict]:
    values = rules.model_dump(mode="json")
    sections = []
    for title, intro, keys in SECTIONS:
        fields = []
        for key in keys:
            value = values[key]
            fields.append({"key": key, "label": loyalty.Policy.model_fields[key].description,
                           "value": value, "hint": HINTS.get(key, ""),
                           "kind": "checkbox" if isinstance(value, bool) else
                                   "text" if key == "name" else "number"})
        sections.append({"title": title, "intro": intro, "fields": fields})
    return sections


def _examples(rules) -> list[dict]:
    """What the current rules give, worked out by the same code a payment uses."""
    rows = []
    for spend in EXAMPLE_SPENDS:
        cells = []
        for number in EXAMPLE_ORDERS:
            if not rules.earning_enabled or spend < rules.minimum_earn_subtotal:
                cells.append({"points": 0, "promo": 0})
                continue
            cells.append({"points": loyalty.earn_points(rules, spend=spend, subtotal=spend, number=number),
                          "promo": loyalty.earn_points(rules, spend=spend, subtotal=spend, number=number,
                                                       promo=True)})
        rows.append({"spend": spend, "cells": cells})
    return rows


@router.get("", name="admin_loyalty")
async def index(request: Request, q: str = "", session: AsyncSession = Depends(get_session),
                current_user: AdminUser = Depends(require_admin)):
    rules = await loyalty.policy(session)
    query = select(Customer, LoyaltyAccount).outerjoin(LoyaltyAccount, LoyaltyAccount.customer_id == Customer.id)
    if q.strip():
        term = f"%{q.strip()}%"
        query = query.where(or_(Customer.name.ilike(term), Customer.whatsapp_number.ilike(term)))
    rows = (await session.execute(query.order_by(Customer.created_at.desc()).limit(50))).all()
    totals = (await session.execute(select(func.count(LoyaltyAccount.customer_id),
                                           func.coalesce(func.sum(LoyaltyAccount.balance), 0))
                                    .where(LoyaltyAccount.balance != 0))).one()
    dishes = list((await session.execute(select(MenuItem.retailer_id, MenuItem.name)
                                         .order_by(MenuItem.name))).all())
    return render(request, "admin/loyalty.html", {
        "current_user": current_user, "rules": rules, "sections": _fields(rules), "rows": rows,
        "query": q, "dishes": dishes, "dish_names": dict(dishes), "days": DAY_LABELS,
        "examples": _examples(rules), "example_orders": EXAMPLE_ORDERS,
        "holders": totals[0], "outstanding": totals[1],
        "kitchens": list((await session.execute(select(Outlet).order_by(Outlet.name))).scalars())})


def _rows(form, *names: str) -> list[tuple[str, ...]]:
    """Repeated inputs (tier_name, tier_orders, ...) zipped into rows, blank rows dropped."""
    columns = [form.getlist(name) for name in names]
    return [tuple(str(v).strip() for v in row) for row in zip(*columns) if any(str(v).strip() for v in row)]


def _number(raw: str, label: str):
    try:
        return Decimal(raw)
    except Exception as exc:  # noqa: BLE001 - any parse failure is the admin's typo
        raise ValueError(f"{label}: enter a number") from exc


@router.post("", name="admin_loyalty_save")
async def save(request: Request, session: AsyncSession = Depends(get_session),
               current_user: AdminUser = Depends(require_admin)):
    form = await request.form()
    # A setting the page does not show (the currency) keeps its saved value.
    current = (await loyalty.policy(session)).model_dump(mode="json")
    try:
        data = {}
        for key, field in loyalty.Policy.model_fields.items():
            if field.annotation is bool:
                data[key] = form.get(key) == "1"
            elif key == "tiers":
                data[key] = [{"name": name, "orders": int(_number(orders, f"Tier {name}")),
                              "multiplier": str(_number(multiplier, f"Tier {name}"))}
                             for name, orders, multiplier in _rows(form, "tier_name", "tier_orders", "tier_multiplier")]
            elif key == "dish_bonuses":
                data[key] = [{"retailer_id": dish, "points": int(_number(points, "Dish bonus"))}
                             for dish, points in _rows(form, "dish_bonus_item", "dish_bonus_points")]
            elif key in {"kitchen_ids", "excluded_items", "promo_days"}:
                data[key] = [str(v) for v in form.getlist(key) if str(v).strip()]
            elif key in {"promo_starts_on", "promo_ends_on"}:
                data[key] = str(form.get(key) or "").strip() or None
            else:
                data[key] = form.get(key) if key in form else current[key]
        await loyalty.save_policy(session, data, current_user.email)
    except (ValueError, TypeError) as exc:
        return redirect(str(request.url_for("admin_loyalty")), flash=("error", f"Settings were not saved: {exc}"))
    return redirect(str(request.url_for("admin_loyalty")), flash=("success", "Loyalty rules saved. Existing payment links keep their agreed rules."))


@router.post("/reset-all", name="admin_loyalty_reset_all")
async def reset_all(request: Request, session: AsyncSession = Depends(get_session),
                    current_user: AdminUser = Depends(require_admin)):
    form = await request.form()
    if str(form.get("confirm") or "").strip().upper() != "RESET":
        return redirect(str(request.url_for("admin_loyalty")),
                        flash=("error", "Nothing was reset. Type RESET to confirm."))
    note = str(form.get("note") or "").strip() or "All balances reset to zero"
    wallets, points = await loyalty.reset_all(session, note=note[:500], actor=current_user.email)
    return redirect(str(request.url_for("admin_loyalty")),
                    flash=("success", f"Reset {wallets} balance(s) to zero ({points} points). Each is in that customer's history."))

@router.get("/{customer_id}", name="admin_loyalty_wallet")
async def wallet(request: Request, customer_id: uuid.UUID, session: AsyncSession = Depends(get_session),
                 current_user: AdminUser = Depends(require_admin)):
    customer = await session.get(Customer, customer_id)
    if customer is None:
        return redirect(str(request.url_for("admin_loyalty")), flash=("error", "Customer not found."))
    return render(request, "admin/loyalty_wallet.html", {
        "current_user": current_user, "customer": customer,
        "info": await loyalty.overview(session, customer_id), "request_id": str(uuid.uuid4())})

@router.post("/{customer_id}/adjust", name="admin_loyalty_adjust")
async def adjust(request: Request, customer_id: uuid.UUID, session: AsyncSession = Depends(get_session),
                 current_user: AdminUser = Depends(require_admin)):
    form = await request.form()
    try:
        if await session.get(Customer, customer_id) is None:
            raise ValueError("Customer not found")
        await loyalty.adjust(session, customer_id, int(str(form.get("points") or "0")),
                             note=str(form.get("note") or ""), actor=current_user.email,
                             request_id=form.get("request_id"))
    except (ValueError, loyalty.LoyaltyError) as exc:
        return redirect(str(request.url_for("admin_loyalty_wallet", customer_id=customer_id)), flash=("error", str(exc)))
    return redirect(str(request.url_for("admin_loyalty_wallet", customer_id=customer_id)), flash=("success", "Points adjustment recorded."))
