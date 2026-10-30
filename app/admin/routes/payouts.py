"""Payouts: the split between the kitchen partner and Shero, and every paid
order with what its kitchen and Shero get, for chosen dates and kitchen.
The money is worked out in services/payouts.py.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone
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
from app.db.models import AdminUser, Outlet
from app.db.session import get_session
from app.services import payouts

log = get_logger(__name__)
router = APIRouter(prefix="/payouts", tags=["admin"])

PERIODS = {"this_month": "This month", "last_month": "Last month", "last_30": "Last 30 days",
           "this_year": "This year", "all": "All dates", "custom": "Choose dates"}
PAGE_SIZE = 100
DEFAULT_ZONE = "America/New_York"
FILTERS = ("period", "start", "end", "kitchen")


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


def _kitchen_id(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


async def _kitchens(session) -> list[Outlet]:
    return list((await session.execute(select(Outlet).order_by(Outlet.name))).scalars())


async def _view(session, *, period, start, end, kitchen):
    kitchens = await _kitchens(session)
    span = _period(period, start, end, _zone(kitchens))
    kitchen_id = _kitchen_id(kitchen)
    split = await payouts.settings(session)
    orders = await payouts.orders_between(session, span["start_utc"], span["end_utc"], kitchen_id)
    return kitchens, span, kitchen_id, split, await payouts.splits(session, orders, split)


@router.get("", name="admin_payouts")
async def payouts_page(
    request: Request,
    period: str | None = None,
    start: str | None = None,
    end: str | None = None,
    kitchen: str | None = None,
    page: int | None = None,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    kitchens, span, kitchen_id, split, items = await _view(
        session, period=period, start=start, end=end, kitchen=kitchen)
    pager = listing.Page.of(page, PAGE_SIZE, len(items))
    return render(request, "admin/payouts.html", {
        "current_user": current_user, "split": split, "kitchens": kitchens,
        "names": {k.id: k.name for k in kitchens}, "span": span, "periods": PERIODS,
        "kitchen_id": kitchen_id, "items": items[pager.offset:pager.offset + PAGE_SIZE],
        "page": pager, "totals": payouts.totals(items), "per_kitchen": payouts.by_kitchen(items),
        "query": {k: v for k in FILTERS if (v := request.query_params.get(k))},
        "url_with": partial(listing.url_with, request),
        "local": partial(_local, _zone(kitchens)),
    })


def _back(request: Request, form) -> str:
    """Back to the page the form was sent from, with its filters."""
    query = urlencode({k: v for k in FILTERS if (v := str(form.get(k) or ""))})
    return str(request.url_for("admin_payouts")) + (f"?{query}" if query else "")


@router.post("/split", name="admin_payouts_split_save")
async def save_split(request: Request, session: AsyncSession = Depends(get_session),
                     current_user: AdminUser = Depends(require_admin)):
    form = await request.form()
    back = _back(request, form)
    own = {}
    for key, value in form.multi_items():
        if key.startswith("kitchen_percent_") and str(value).strip():
            own[key.removeprefix("kitchen_percent_")] = str(value).strip()
    try:
        saved = await payouts.save(session, {
            "same_for_all": form.get("same_for_all") == "on",
            "kitchen_percent": str(form.get("kitchen_percent") or "").strip() or "0",
            "kitchens": own}, current_user.email)
    except payouts.PayoutError as exc:
        return redirect(back, flash=("error", f"The split was not saved: {exc}"))
    log.info("payout_split_saved", by=current_user.email, same_for_all=saved.same_for_all,
             percent=str(saved.kitchen_percent), kitchens=len(saved.kitchens))
    return redirect(back, flash=("success", (
        f"Saved: kitchen partner {saved.kitchen_percent.normalize():f}%, Shero "
        f"{saved.shero_percent.normalize():f}%, for every kitchen." if saved.same_for_all
        else "Saved: each kitchen's own split.")))


@router.get("/orders.csv", name="admin_payouts_csv")
async def orders_csv(request: Request, period: str | None = None, start: str | None = None,
                     end: str | None = None, kitchen: str | None = None,
                     session: AsyncSession = Depends(get_session),
                     current_user: AdminUser = Depends(require_admin)):
    kitchens, span, _, _, items = await _view(session, period=period, start=start, end=end,
                                              kitchen=kitchen)
    body = payouts.orders_csv(items, {k.id: k.name for k in kitchens})
    stamp = (span["label"] or "orders").replace(" ", "-").replace("–", "to")
    return Response(content=body, media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="kitchen-payouts-{stamp}.csv"'})
