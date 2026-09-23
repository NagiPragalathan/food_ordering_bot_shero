"""Admin dashboard: the numbers worth seeing on arrival."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import render, require_admin
from app.core.config import settings
from app.db.models import (
    AdminUser,
    Category,
    Cuisine,
    Customer,
    LeadStage,
    MenuItem,
    Order,
    OrderStage,
    PaymentStatus,
)
from app.db.session import get_session

router = APIRouter(tags=["admin"])

# Stages shown in the drop-off funnel, in flow order (spec section 2).
FUNNEL = [
    LeadStage.NEW_ENQUIRY,
    LeadStage.DETAILS_CAPTURED,
    LeadStage.CUISINE_SELECTED,
    LeadStage.CART_CREATED,
    LeadStage.OUTLET_SELECTED,
    LeadStage.SLOT_SELECTED,
    LeadStage.PAYMENT_LINK_SENT,
    LeadStage.CONVERTED,
]

ACTIVE_ORDER_STAGES = [
    OrderStage.PAID_SLOT_BOOKED,
    OrderStage.SENT_TO_KITCHEN,
    OrderStage.OUT_FOR_DELIVERY,
]


@router.get("/", name="admin_dashboard")
async def dashboard(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    since = datetime.now(timezone.utc) - timedelta(days=30)

    paid_orders = await session.scalar(
        select(func.count(Order.id)).where(Order.payment_status == PaymentStatus.PAID)
    )
    revenue = await session.scalar(
        select(func.coalesce(func.sum(Order.total), 0)).where(
            Order.payment_status == PaymentStatus.PAID
        )
    )
    revenue_30d = await session.scalar(
        select(func.coalesce(func.sum(Order.total), 0)).where(
            Order.payment_status == PaymentStatus.PAID, Order.paid_at >= since
        )
    )
    customers = await session.scalar(select(func.count(Customer.id)))
    converted = await session.scalar(
        select(func.count(Customer.id)).where(Customer.is_converted.is_(True))
    )
    items_live = await session.scalar(
        select(func.count(MenuItem.id)).where(MenuItem.is_available.is_(True))
    )
    cuisine_count = await session.scalar(
        select(func.count(Cuisine.id)).where(Cuisine.is_active.is_(True))
    )
    category_count = await session.scalar(select(func.count(Category.id)))

    funnel = await _funnel_counts(session)

    active = await session.execute(
        select(Order)
        .where(Order.stage.in_([str(s) for s in ACTIVE_ORDER_STAGES]))
        .order_by(Order.created_at.desc())
        .limit(8)
    )
    recent = await session.execute(
        select(Order).order_by(Order.created_at.desc()).limit(8)
    )

    return render(request, "admin/dashboard.html", {
        "current_user": current_user,
        "stats": {
            "paid_orders": int(paid_orders or 0),
            "revenue": Decimal(revenue or 0),
            "revenue_30d": Decimal(revenue_30d or 0),
            "customers": int(customers or 0),
            "converted": int(converted or 0),
            "items_live": int(items_live or 0),
            "cuisines": int(cuisine_count or 0),
            "categories": int(category_count or 0),
        },
        "funnel": funnel,
        "active_orders": list(active.scalars()),
        "recent_orders": list(recent.scalars()),
        "missing_credentials": settings.missing_credentials(),
    })


async def _funnel_counts(session: AsyncSession) -> list[dict]:
    """Customers who reached *at least* each stage (spec section 2).

    `stage_timestamps` records every stage a customer passed through, so a key
    check is more honest than comparing only their current stage - somebody
    who paid still counts as having reached "Cart Created".

    The JSONB key operator is Postgres-only, so SQLite (used by the tests)
    falls back to counting in Python.
    """
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        counts = {}
        for stage in FUNNEL:
            counts[str(stage)] = int(await session.scalar(
                select(func.count(Customer.id)).where(
                    Customer.stage_timestamps.has_key(str(stage))  # noqa: W601
                )
            ) or 0)
    else:
        result = await session.execute(select(Customer.stage_timestamps))
        histories = [row or {} for row in result.scalars()]
        counts = {
            str(stage): sum(1 for h in histories if str(stage) in h)
            for stage in FUNNEL
        }

    funnel = [{"stage": str(s), "count": counts[str(s)]} for s in FUNNEL]
    top = funnel[0]["count"] if funnel else 0
    for row in funnel:
        row["percent"] = round(row["count"] / top * 100) if top else 0
    return funnel
