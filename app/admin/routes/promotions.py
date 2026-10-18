"""Offers & Promotions: coupon codes customers apply at checkout.

The list shows every promotion with how it is doing (uses, discount given,
sales it brought in); each one opens in an editor with a live preview of the
offer as customers see it, and the orders that used it. The rules and the
money are in services/promotions.py; parsing the form is in
services/promotion_admin.py.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import redirect, render, require_admin
from app.core.logging import get_logger
from app.db.models import AdminUser, Customer, Order, Outlet, Promotion
from app.db.session import get_session
from app.services import promotion_admin, promotions, receipts

log = get_logger(__name__)
router = APIRouter(prefix="/promotions", tags=["admin"])

RECENT_ORDERS = 25


@router.get("", name="admin_promotions")
async def promotions_page(
    request: Request,
    status: str = "",
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    rows = list((await session.execute(
        select(Promotion).order_by(Promotion.display_order, Promotion.created_at.desc())
    )).scalars())
    used = await promotions.usage(session, [p.id for p in rows])
    today = await promotions.local_today(session)
    kitchens = {k.id: k for k in (await session.execute(select(Outlet))).scalars()}

    cards = []
    for promotion in rows:
        usage = used[promotion.id]
        cards.append({
            "p": promotion,
            "usage": usage,
            "status": promotions.status(promotion, today=today, used=usage.used),
            "headline": promotions.headline(promotion),
            "tag": promotion.tag or promotions.default_tag(promotion),
            "terms": promotions.conditions(promotion, with_end=False),
            "kitchen": kitchens.get(promotion.outlet_id) if promotion.outlet_id else None,
        })
    counts: dict[str, int] = {}
    for card in cards:
        counts[card["status"]] = counts.get(card["status"], 0) + 1
    shown = [c for c in cards if not status or c["status"] == status]

    return render(request, "admin/promotions.html", {
        "current_user": current_user,
        "cards": shown,
        "all_count": len(cards),
        "counts": counts,
        "selected_status": status,
        "status_labels": promotions.STATUS_LABELS,
        "placements": {value: label for value, label, _ in promotion_admin.PLACEMENTS},
        "totals": {
            "live": counts.get(promotions.LIVE, 0),
            "redemptions": sum(c["usage"].paid for c in cards),
            "held": sum(c["usage"].held for c in cards),
            "discount": sum((c["usage"].discount_given for c in cards), promotions.ZERO),
            "revenue": sum((c["usage"].revenue for c in cards), promotions.ZERO),
        },
    })


@router.get("/new", name="admin_promotion_new")
async def new_promotion(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    return await _form(request, session, current_user, None, promotion_admin.defaults())


@router.post("", name="admin_promotion_create")
async def create_promotion(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    return await _save(request, session, current_user, None)


@router.get("/{promotion_id}", name="admin_promotion_edit")
async def edit_promotion(
    request: Request,
    promotion_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    promotion = await session.get(Promotion, promotion_id)
    if promotion is None:
        return _gone(request)
    return await _form(request, session, current_user, promotion,
                       promotion_admin.values_of(promotion))


@router.post("/{promotion_id}", name="admin_promotion_update")
async def update_promotion(
    request: Request,
    promotion_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    promotion = await session.get(Promotion, promotion_id)
    if promotion is None:
        return _gone(request)
    return await _save(request, session, current_user, promotion)


@router.post("/{promotion_id}/toggle", name="admin_promotion_toggle")
async def toggle_promotion(
    request: Request,
    promotion_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    promotion = await session.get(Promotion, promotion_id)
    if promotion is None:
        return _gone(request)
    promotion.is_active = not promotion.is_active
    await session.flush()
    log.info("promotion_toggled", code=promotion.code, active=promotion.is_active,
             by=current_user.email)
    state = "switched on" if promotion.is_active else "paused - customers can no longer use it"
    return redirect(_back(request), flash=("success", f"{promotion.code} {state}."))


@router.post("/{promotion_id}/duplicate", name="admin_promotion_duplicate")
async def duplicate_promotion(
    request: Request,
    promotion_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    source = await session.get(Promotion, promotion_id)
    if source is None:
        return _gone(request)
    try:
        copy = await promotion_admin.duplicate(session, source, by=current_user.email)
    except promotion_admin.PromotionFormError as exc:
        return redirect(_back(request), flash=("error", str(exc)))
    log.info("promotion_duplicated", source=source.code, code=copy.code, by=current_user.email)
    return redirect(str(request.url_for("admin_promotion_edit", promotion_id=copy.id)),
                    flash=("success", f"Copied as {copy.code}. It is paused until you switch it on."))


@router.post("/{promotion_id}/delete", name="admin_promotion_delete")
async def delete_promotion(
    request: Request,
    promotion_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    promotion = await session.get(Promotion, promotion_id)
    if promotion is None:
        return _gone(request)
    code = promotion.code
    await session.delete(promotion)
    await session.flush()
    log.info("promotion_deleted", code=code, by=current_user.email)
    # Orders keep their own copy of the code and discount, so bills are unchanged.
    return redirect(str(request.url_for("admin_promotions")),
                    flash=("success", f"Deleted {code}. Orders that used it keep their discount."))


# --- helpers -----------------------------------------------------------------
async def _form(request: Request, session: AsyncSession, current_user: AdminUser,
                promotion: Promotion | None, values: dict, *, error: str | None = None):
    kitchens = list((await session.execute(
        select(Outlet).order_by(Outlet.is_primary.desc(), Outlet.created_at)
    )).scalars())
    context = {
        "current_user": current_user,
        "promotion": promotion,
        "v": values,
        "error": error,
        "kitchens": kitchens,
        "discount_types": promotion_admin.DISCOUNT_TYPES,
        "placements": promotion_admin.PLACEMENTS,
        "usage": None,
        "status": None,
        "recent": [],
    }
    if promotion is not None:
        usage = (await promotions.usage(session, [promotion.id]))[promotion.id]
        context["usage"] = usage
        context["status"] = promotions.status(
            promotion, today=await promotions.local_today(session), used=usage.used)
        context["status_label"] = promotions.STATUS_LABELS[context["status"]]
        context["recent"] = await _recent_orders(session, promotion)
    return render(request, "admin/promotion_form.html", context,
                  status_code=400 if error else 200)


async def _save(request: Request, session: AsyncSession, current_user: AdminUser,
                promotion: Promotion | None):
    form = await request.form()
    try:
        data = promotion_admin.parse_form(form)
        saved = await promotion_admin.save(session, promotion, data, by=current_user.email)
    except promotion_admin.PromotionFormError as exc:
        # Shown on the form itself, with everything typed kept.
        return await _form(request, session, current_user, promotion,
                           promotion_admin.posted_values(form), error=str(exc))

    log.info("promotion_saved", code=saved.code, created=promotion is None,
             type=saved.discount_type, value=str(saved.discount_value),
             active=saved.is_active, by=current_user.email)
    verb = "Created" if promotion is None else "Saved"
    note = "" if saved.is_active else " It is paused, so customers cannot use it yet."
    return redirect(str(request.url_for("admin_promotions")),
                    flash=("success", f"{verb} {saved.code}.{note}"))


async def _recent_orders(session: AsyncSession, promotion: Promotion) -> list[dict]:
    """The latest orders that carried this code, paid or not."""
    rows = (await session.execute(
        select(Order, Customer)
        .join(Customer, Customer.id == Order.customer_id)
        .where(Order.promotion_id == promotion.id)
        .order_by(Order.created_at.desc())
        .limit(RECENT_ORDERS)
    )).all()
    return [{"order": order, "customer": customer,
             "bill_url": _bill_url(order) if order.is_paid else None}
            for order, customer in rows]


def _bill_url(order: Order) -> str | None:
    """The customer's bill link, or None where links cannot be signed
    (no ADMIN_SESSION_SECRET) - the page must still open."""
    try:
        return receipts.receipt_url(order.order_number)
    except RuntimeError:
        return None


def _back(request: Request) -> str:
    """Where a toggle or copy came from: the list (with its filter) or the editor."""
    referer = request.headers.get("referer") or ""
    base = str(request.url_for("admin_promotions"))
    return referer if referer.startswith(base) else base


def _gone(request: Request):
    return redirect(str(request.url_for("admin_promotions")),
                    flash=("error", "That promotion no longer exists."))
