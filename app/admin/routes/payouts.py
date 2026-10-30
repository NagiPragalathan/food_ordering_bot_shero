"""Payouts: how each order's money is split between its kitchen and Shero,
what each kitchen is owed, and the payments made to kitchens.

Three tabs, in the URL (?tab=): the overview for a period, the split rules,
and the payout history. The money itself is worked out in services/payouts.py.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from functools import partial
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import listing
from app.admin.deps import redirect, render, require_admin
from app.core.logging import get_logger
from app.db.models import AdminUser, Order, Outlet, PartnerPayout
from app.db.session import get_session
from app.services import payouts

log = get_logger(__name__)
router = APIRouter(prefix="/payouts", tags=["admin"])

TABS = {"overview": "Overview", "rules": "Split rules", "history": "Payout history"}
PERIODS = {"this_month": "This month", "last_month": "Last month", "last_30": "Last 30 days",
           "this_year": "This year", "all": "All dates", "custom": "Choose dates"}
STATUS_FILTERS = {"": "All orders", **payouts.STATUSES}
PAGE_SIZE = 50
DEFAULT_ZONE = "America/New_York"
# Order amounts shown in the split examples.
EXAMPLES = (Decimal(50), Decimal(100), Decimal(250))


def _zone(kitchens: list[Outlet]) -> ZoneInfo:
    """Dates on the page are the kitchens' local dates."""
    for kitchen in kitchens:
        try:
            return ZoneInfo(kitchen.timezone or DEFAULT_ZONE)
        except Exception:  # noqa: BLE001 - a bad name falls through to the default
            continue
    return ZoneInfo(DEFAULT_ZONE)


def _local(zone: ZoneInfo, when: datetime | None) -> str:
    if when is None:
        return ""
    when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)   # SQLite: naive UTC
    return when.astimezone(zone).strftime("%d %b %Y, %I:%M %p")


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "").strip()) if (value or "").strip() else None
    except ValueError:
        return None


def _period(period: str | None, start: str | None, end: str | None, zone: ZoneInfo) -> dict:
    """The dates a view covers, and the same as UTC times for the query."""
    key = listing.choice(period, PERIODS, "this_month")
    today = datetime.now(zone).date()
    first = today.replace(day=1)
    match key:
        case "this_month":
            frm, to = first, today
        case "last_month":
            to = first - timedelta(days=1)
            frm = to.replace(day=1)
        case "last_30":
            frm, to = today - timedelta(days=29), today
        case "this_year":
            frm, to = today.replace(month=1, day=1), today
        case "all":
            frm = to = None
        case _:
            frm, to = _date(start), _date(end)
            if frm and to and to < frm:
                frm, to = to, frm

    def utc(day: date | None):
        return None if day is None else datetime.combine(day, time(0), zone).astimezone(timezone.utc)

    if key == "all":
        label = "All dates"
    elif frm and to:
        label = (f"{frm:%d %b %Y}" if frm == to else f"{frm:%d %b} – {to:%d %b %Y}")
    else:
        label = (f"From {frm:%d %b %Y}" if frm else f"Up to {to:%d %b %Y}" if to else "All dates")
    return {"key": key, "label": label, "start": frm, "end": to,
            "start_utc": utc(frm), "end_utc": utc(to + timedelta(days=1)) if to else None}


def _query(request: Request, **changes) -> dict:
    """The page's filters, for links and hidden form fields."""
    params = {k: request.query_params.get(k, "") for k in ("period", "start", "end", "kitchen", "status")}
    params.update(changes)
    return {k: v for k, v in params.items() if v}


def _kitchen_id(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


async def _kitchens(session) -> list[Outlet]:
    return list((await session.execute(select(Outlet).order_by(Outlet.name))).scalars())


async def _view(session, request: Request, *, period: str | None, start: str | None,
                end: str | None, kitchen: str | None):
    kitchens = await _kitchens(session)
    span = _period(period, start, end, _zone(kitchens))
    kitchen_id = _kitchen_id(kitchen)
    rules = await payouts.rules(session)
    orders = await payouts.orders_between(session, span["start_utc"], span["end_utc"], kitchen_id)
    return kitchens, span, kitchen_id, rules, await payouts.splits(session, orders, rules)


def _examples(rules: payouts.Rules, kitchens: list[Outlet], coverage: dict) -> list[dict]:
    """What a kitchen gets from an order of a few sizes, under each rule."""
    rows = []
    for kitchen in [None, *kitchens]:
        if kitchen is not None and not rules.has_own_rule(kitchen.id):
            continue
        basis, percent = rules.rule_for(kitchen.id if kitchen else None)
        share = percent
        if basis == "dish_price" and coverage["average_percent"] is not None:
            share = coverage["average_percent"]
        rows.append({"name": kitchen.name if kitchen else "Default rule",
                     "rule": payouts.describe(basis, percent), "estimate": basis == "dish_price",
                     "cells": [{"food": food, "kitchen": payouts.money(food * share / 100),
                                "company": payouts.money(food - food * share / 100)}
                               for food in EXAMPLES]})
    return rows


@router.get("", name="admin_payouts")
async def payouts_page(
    request: Request,
    tab: str | None = None,
    period: str | None = None,
    start: str | None = None,
    end: str | None = None,
    kitchen: str | None = None,
    status: str | None = None,
    page: int | None = None,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    tab = listing.choice(tab, TABS, "overview")
    kitchens, span, kitchen_id, rules, items = await _view(
        session, request, period=period, start=start, end=end, kitchen=kitchen)
    names = {k.id: k.name for k in kitchens}
    context = {
        "current_user": current_user, "tab": tab, "tabs": TABS, "rules": rules,
        "kitchens": kitchens, "names": names, "span": span, "periods": PERIODS,
        "kitchen_id": kitchen_id, "query": _query(request),
        "url_with": partial(listing.url_with, request),
        "bases": payouts.BASES, "discount_options": payouts.DISCOUNTS,
        "payable_options": payouts.PAYABLE, "describe": payouts.describe,
        "today": datetime.now(_zone(kitchens)).date(),
        "local": partial(_local, _zone(kitchens)),
    }

    if tab == "overview":
        chosen = listing.choice(status, STATUS_FILTERS, "")
        shown = [s for s in items if not chosen or s.status == chosen]
        pager = listing.Page.of(page, PAGE_SIZE, len(shown))
        start_at = (pager.number - 1) * PAGE_SIZE
        # What is owed for every date, not just this view's.
        unpaid = await payouts.splits(session, [o for o in await payouts.orders_between(
            session, None, None, kitchen_id) if not o.payout_id], rules)
        payout_ids = {s.order.payout_id for s in shown[start_at:start_at + PAGE_SIZE] if s.order.payout_id}
        context.update({
            "items": shown[start_at:start_at + PAGE_SIZE], "page": pager,
            "totals": payouts.totals(items), "per_kitchen": payouts.by_kitchen(items),
            "all_due": payouts.totals(s for s in unpaid if s.status == "due"),
            "loose": sum(1 for s in items if not s.locked and not s.order.payout_id),
            "status": chosen, "status_options": STATUS_FILTERS,
            "status_counts": {key: sum(1 for s in items if not key or s.status == key)
                              for key in STATUS_FILTERS},
            "payout_refs": {p.id: p for p in (await session.execute(
                select(PartnerPayout).where(PartnerPayout.id.in_(payout_ids)))).scalars()}
            if payout_ids else {},
        })
    elif tab == "rules":
        coverage = await payouts.ppp_coverage(session)
        context.update({"coverage": coverage, "examples": _examples(rules, kitchens, coverage),
                        "own": {r.outlet_id: r for r in rules.kitchens}})
    else:
        context["history"] = list((await session.execute(
            select(PartnerPayout).order_by(PartnerPayout.created_at.desc()).limit(200))).scalars())
    return render(request, "admin/payouts.html", context)


def _back(request: Request, form, **extra) -> str:
    """Back to the page the form was sent from, with its filters."""
    params = {k: str(form.get(k) or "") for k in ("tab", "period", "start", "end", "kitchen", "status")}
    params.update(extra)
    query = urlencode({k: v for k, v in params.items() if v})
    return str(request.url_for("admin_payouts")) + (f"?{query}" if query else "")


@router.post("/rules", name="admin_payouts_rules_save")
async def save_rules(request: Request, session: AsyncSession = Depends(get_session),
                     current_user: AdminUser = Depends(require_admin)):
    form = await request.form()
    back = _back(request, form, tab="rules")
    try:
        own = []
        for outlet_id, basis, percent in zip(form.getlist("rule_kitchen"), form.getlist("rule_basis"),
                                             form.getlist("rule_percent")):
            basis, percent = str(basis).strip(), str(percent).strip()
            if basis or percent:
                own.append({"outlet_id": str(outlet_id), "basis": basis or None,
                            "percent": percent or None})
        saved = await payouts.save_rules(session, {
            "basis": str(form.get("basis") or "percent"),
            "kitchen_percent": str(form.get("kitchen_percent") or "").strip() or "0",
            "discounts": str(form.get("discounts") or "company"),
            "payable_after": str(form.get("payable_after") or "delivered"),
            "kitchens": own}, current_user.email)
    except (ValueError, TypeError) as exc:
        return redirect(back, flash=("error", f"The split was not saved: {_reason(exc)}"))
    log.info("payout_rules_saved", by=current_user.email, basis=saved.basis,
             percent=str(saved.kitchen_percent), kitchens=len(saved.kitchens))
    return redirect(back, flash=("success", "Split saved. It applies to orders paid from now on; "
                                            "orders already paid keep the split they were paid under."))


def _reason(exc: Exception) -> str:
    """A pydantic error as one readable line."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        parts = []
        for error in errors():
            where = " ".join(str(p) for p in error.get("loc", ()) if not isinstance(p, int))
            parts.append(f"{where.replace('_', ' ')}: {error.get('msg', '')}".strip(": "))
        return "; ".join(parts)
    return str(exc)


@router.post("/pay", name="admin_payouts_pay")
async def record_payout(request: Request, session: AsyncSession = Depends(get_session),
                        current_user: AdminUser = Depends(require_admin)):
    form = await request.form()
    back = _back(request, form)
    outlet = await session.get(Outlet, _kitchen_id(form.get("pay_kitchen")) or uuid.uuid4())
    if outlet is None:
        return redirect(back, flash=("error", "That kitchen no longer exists."))
    paid_on = _date(form.get("paid_on")) or date.today()
    *_, items = await _view(session, request, period=form.get("period"), start=form.get("start"),
                            end=form.get("end"), kitchen=str(outlet.id))
    due = [s for s in items if s.status == "due"]
    # The page showed an amount; refuse if it has changed since (a refund, an
    # order delivered meanwhile), so what is recorded is what was agreed.
    expected = str(form.get("expected") or "")
    if expected != str(payouts.totals(due).due):
        return redirect(back, flash=("warning", f"What is due to {outlet.name} changed since the page "
                                                f"loaded. Nothing was recorded; check the new amount."))
    try:
        payout = await payouts.pay(session, outlet, due, paid_on=paid_on,
                                   reference=str(form.get("reference") or ""),
                                   note=str(form.get("note") or ""), actor=current_user.email)
    except payouts.PayoutError as exc:
        return redirect(back, flash=("error", str(exc)))
    log.info("partner_payout_recorded", kitchen=outlet.code, amount=str(payout.amount),
             orders=payout.order_count, by=current_user.email)
    return redirect(back, flash=("success", f"Recorded ${payout.amount:.2f} paid to {outlet.name} "
                                            f"for {payout.order_count} order(s)."))


@router.post("/apply-rules", name="admin_payouts_apply")
async def apply_rules(request: Request, session: AsyncSession = Depends(get_session),
                      current_user: AdminUser = Depends(require_admin)):
    form = await request.form()
    kitchens = await _kitchens(session)
    span = _period(form.get("period"), form.get("start"), form.get("end"), _zone(kitchens))
    orders = await payouts.orders_between(session, span["start_utc"], span["end_utc"],
                                          _kitchen_id(form.get("kitchen")))
    count = await payouts.apply_rules(session, orders, current_user.email)
    log.info("payout_rules_applied", orders=count, by=current_user.email)
    return redirect(_back(request, form), flash=(
        "success", f"Today's split now applies to {count} order(s) not paid out yet. "
                   "Amounts set by hand were kept."))


@router.post("/orders/{order_number}", name="admin_payouts_order")
async def set_order_amount(request: Request, order_number: str,
                           session: AsyncSession = Depends(get_session),
                           current_user: AdminUser = Depends(require_admin)):
    form = await request.form()
    back = _back(request, form)
    order = (await session.execute(select(Order).where(
        Order.order_number == order_number.upper()))).scalar_one_or_none()
    if order is None:
        return redirect(back, flash=("error", f"Order {order_number} not found."))
    amount = "" if form.get("clear") else str(form.get("amount") or "")
    try:
        split = await payouts.set_amount(session, order, amount, str(form.get("note") or ""),
                                         current_user.email)
    except payouts.PayoutError as exc:
        return redirect(back, flash=("error", str(exc)))
    log.info("payout_order_amount_set", order_number=order.order_number,
             amount=amount or "rule", by=current_user.email)
    return redirect(back, flash=("success", f"{order.order_number}: the kitchen gets "
                                            f"${split.kitchen:.2f}, Shero ${split.company:.2f}."))


@router.post("/{payout_id}/undo", name="admin_payouts_undo")
async def undo_payout(request: Request, payout_id: uuid.UUID,
                      session: AsyncSession = Depends(get_session),
                      current_user: AdminUser = Depends(require_admin)):
    form = await request.form()
    back = _back(request, form, tab="history")
    payout = await session.get(PartnerPayout, payout_id)
    if payout is None:
        return redirect(back, flash=("error", "That payout was not found."))
    try:
        count = await payouts.undo(session, payout, current_user.email)
    except payouts.PayoutError as exc:
        return redirect(back, flash=("error", str(exc)))
    log.info("partner_payout_undone", payout=str(payout.id), by=current_user.email)
    return redirect(back, flash=("success", f"Payout to {payout.outlet_name} undone; its {count} "
                                            "order(s) are due again."))


def _csv(text: str, filename: str) -> Response:
    return Response(text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/statement.csv", name="admin_payouts_csv")
async def statement(request: Request, period: str | None = None, start: str | None = None,
                    end: str | None = None, kitchen: str | None = None, status: str | None = None,
                    session: AsyncSession = Depends(get_session),
                    current_user: AdminUser = Depends(require_admin)):
    kitchens, span, kitchen_id, _, items = await _view(
        session, request, period=period, start=start, end=end, kitchen=kitchen)
    chosen = listing.choice(status, STATUS_FILTERS, "")
    items = [s for s in items if not chosen or s.status == chosen]
    ids = {s.order.payout_id for s in items if s.order.payout_id}
    refs = {p.id: p for p in (await session.execute(
        select(PartnerPayout).where(PartnerPayout.id.in_(ids)))).scalars()} if ids else {}
    names = {k.id: k.name for k in kitchens}
    who = names.get(kitchen_id, "all-kitchens").lower().replace(" ", "-")
    when = span["key"] if not span["start"] else f"{span['start']}_{span['end'] or 'now'}"
    return _csv(payouts.statement_csv(items, names, refs), f"payouts-{who}-{when}.csv")


@router.get("/{payout_id}/statement.csv", name="admin_payout_csv")
async def payout_statement(request: Request, payout_id: uuid.UUID,
                           session: AsyncSession = Depends(get_session),
                           current_user: AdminUser = Depends(require_admin)):
    payout = await session.get(PartnerPayout, payout_id)
    if payout is None:
        return redirect(str(request.url_for("admin_payouts")) + "?tab=history",
                        flash=("error", "That payout was not found."))
    orders = list((await session.execute(select(Order).where(Order.payout_id == payout.id)
                                         .order_by(Order.paid_at))).scalars())
    names = {k.id: k.name for k in await _kitchens(session)}
    names.setdefault(payout.outlet_id, payout.outlet_name)
    text = payouts.statement_csv(await payouts.splits(session, orders), names, {payout.id: payout})
    return _csv(text, f"payout-{payout.outlet_name.lower().replace(' ', '-')}-{payout.paid_on}.csv")
