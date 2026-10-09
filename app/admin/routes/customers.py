"""Customers: everyone who has messaged the bot, with Delete and Push to Zoho.

See services/customer_admin.py for what each action touches.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import partial

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import listing
from app.admin.deps import redirect, render, require_admin
from app.core.logging import get_logger
from app.db.models import AdminUser, LeadStage
from app.db.session import get_session
from app.services import customer_admin

log = get_logger(__name__)
router = APIRouter(prefix="/customers", tags=["admin"])


PER_PAGE = 25


@router.get("", name="admin_customers")
async def customers_index(
    request: Request,
    q: str | None = None,
    stage: str | None = None,
    activity: str | None = None,
    zoho: str | None = None,
    sort: str | None = None,
    page: str | None = None,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    filters = {
        "stage": listing.choice(stage, {str(s) for s in LeadStage}, "") or None,
        "activity": listing.choice(activity, customer_admin.ACTIVITY, "") or None,
        "zoho": listing.choice(zoho, customer_admin.ZOHO_FILTERS, "") or None,
    }
    order = listing.choice(sort, customer_admin.SORTS, "recent")
    total = await customer_admin.count_customers(session, q, **filters)
    pager = listing.Page.of(page, PER_PAGE, total)
    rows = await customer_admin.list_customers(
        session, q, PER_PAGE, verify=True, offset=pager.offset, sort=order, **filters)
    return render(request, "admin/customers.html", {
        "current_user": current_user,
        "rows": rows,
        "query": q or "",
        "filters": filters,
        "sort": order,
        "page": pager,
        "filtering": bool(q or any(filters.values())),
        "stats": await customer_admin.customer_stats(
            session, since=datetime.now(timezone.utc) - timedelta(days=7)),
        "stages": [str(s) for s in LeadStage],
        "activity_options": customer_admin.ACTIVITY,
        "zoho_options": customer_admin.ZOHO_FILTERS,
        "sort_options": customer_admin.SORTS,
        "url_with": partial(listing.url_with, request),
        "ago": listing.ago,
        "zoho_connected": customer_admin.zoho_connected(),
        "unsynced": sum(1 for r in rows if r.needs_push),
    })


@router.post("/push-all", name="admin_customers_push_all")
async def push_all(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    """Push every customer on the page that is not fully in Zoho yet."""
    url = str(request.url_for("admin_customers"))
    if not customer_admin.zoho_connected():
        return redirect(url, flash=("error", "Connect Zoho in Settings first."))
    pending = [r.customer
               for r in await customer_admin.list_customers(session, verify=True)
               if r.needs_push]
    if not pending:
        return redirect(url, flash=("success", "Everyone is already in Zoho."))

    failed = []
    for customer in pending:
        if await customer_admin.push_to_zoho(session, customer):
            failed.append(customer.whatsapp_number)
    log.info("admin_customers_push_all", total=len(pending), failed=len(failed),
             by=current_user.email)
    if failed:
        return redirect(url, flash=(
            "warning",
            f"Pushed {len(pending) - len(failed)} of {len(pending)}. Failed: "
            f"{', '.join(failed)} - push them one by one to see why.",
        ))
    return redirect(url, flash=("success", f"Pushed {len(pending)} customer(s) to Zoho."))


@router.post("/{customer_id}/push", name="admin_customer_push")
async def push_one(
    request: Request,
    customer_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    url = str(request.url_for("admin_customers"))
    if not customer_admin.zoho_connected():
        return redirect(url, flash=("error", "Connect Zoho in Settings first."))
    customer = await customer_admin.get(session, customer_id)
    if customer is None:
        return redirect(url, flash=("error", "That customer no longer exists."))

    problems = await customer_admin.push_to_zoho(session, customer)
    who = customer.name or customer.whatsapp_number
    if problems:
        return redirect(url, flash=("error", f"{who}: " + " ".join(problems)))
    return redirect(url, flash=("success", f"{who} is up to date in Zoho."))


@router.post("/{customer_id}/delete", name="admin_customer_delete")
async def delete_one(
    request: Request,
    customer_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    url = str(request.url_for("admin_customers"))
    customer = await customer_admin.get(session, customer_id)
    if customer is None:
        return redirect(url, flash=("error", "That customer no longer exists."))

    who = customer.name or customer.whatsapp_number
    removed = await customer_admin.delete_customer(session, customer)
    log.info("admin_customer_delete", by=current_user.email)
    return redirect(url, flash=(
        "success",
        f"Deleted {who} with {removed['orders']} order(s) and "
        f"{removed['addresses']} address(es). Their next message starts fresh. "
        "Zoho was not changed.",
    ))
