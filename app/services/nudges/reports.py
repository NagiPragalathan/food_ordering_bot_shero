"""The send log and what the nudges earned.

A conversion is a paid order within `attribution_days` of a nudge, credited
to the latest nudge before it (last touch) - so two campaigns reaching the
same customer never both claim one order.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Customer, NudgeSend, NudgeStatus, Order, PaymentStatus
from app.services.nudges import store
from app.services.nudges.audience import aware

ZERO = Decimal("0.00")
MAX_LIMIT = 500


def send_view(row: NudgeSend) -> dict:
    return {
        "id": str(row.id),
        "campaign_id": str(row.campaign_id) if row.campaign_id else None,
        "campaign": row.campaign_name,
        "kind": row.kind,
        "customer_id": str(row.customer_id),
        "whatsapp_number": row.whatsapp_number,
        "status": row.status,
        "channel": row.channel,
        "message": row.message,
        "promo_code": row.promo_code,
        "error": row.error,
        "zoho_synced": row.zoho_synced,
        "zoho_error": row.zoho_error,
        "actor": row.actor,
        "dedupe_key": row.dedupe_key,
        "sent_at": aware(row.sent_at).isoformat() if row.sent_at else None,
        "created_at": aware(row.created_at).isoformat() if row.created_at else None,
    }


async def list_sends(session: AsyncSession, *, campaign_id: uuid.UUID | None = None,
                     customer_id: uuid.UUID | None = None, status: str | None = None,
                     since: datetime | None = None, limit: int = 50, offset: int = 0) -> dict:
    query = select(NudgeSend)
    count = select(func.count(NudgeSend.id))
    filters = []
    if campaign_id:
        filters.append(NudgeSend.campaign_id == campaign_id)
    if customer_id:
        filters.append(NudgeSend.customer_id == customer_id)
    if status:
        filters.append(NudgeSend.status == status)
    if since:
        filters.append(NudgeSend.created_at >= since)
    if filters:
        query, count = query.where(*filters), count.where(*filters)
    limit = max(1, min(limit, MAX_LIMIT))
    rows = (await session.execute(
        query.order_by(NudgeSend.created_at.desc()).limit(limit).offset(max(offset, 0)))).scalars()
    return {"total": (await session.execute(count)).scalar_one(),
            "limit": limit, "offset": offset, "sends": [send_view(r) for r in rows]}


async def stats(session: AsyncSession, *, days: int = 30, campaign_id: uuid.UUID | None = None,
                now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    _, settings = await store.load_settings(session)
    window = timedelta(days=settings.attribution_days)

    query = select(NudgeSend).where(
        NudgeSend.status.in_([NudgeStatus.SENT, NudgeStatus.FAILED, NudgeStatus.DRY_RUN]),
        NudgeSend.created_at >= since)
    if campaign_id:
        query = query.where(NudgeSend.campaign_id == campaign_id)
    sends = list((await session.execute(query)).scalars())

    per: dict[str, dict] = {}
    delivered: dict[uuid.UUID, list[NudgeSend]] = defaultdict(list)
    for row in sends:
        key = str(row.campaign_id) if row.campaign_id else f"deleted:{row.campaign_name}"
        entry = per.setdefault(key, {
            "campaign_id": str(row.campaign_id) if row.campaign_id else None,
            "campaign": row.campaign_name, "kind": row.kind, "sent": 0, "failed": 0,
            "dry_run": 0, "customers": set(), "conversions": 0, "revenue": ZERO,
            "coupon_uses": 0, "zoho_synced": 0})
        entry[str(row.status)] += 1
        if row.status == NudgeStatus.SENT:
            entry["customers"].add(row.customer_id)
            entry["zoho_synced"] += int(bool(row.zoho_synced))
            delivered[row.customer_id].append(row)

    # Last touch: each paid order goes to the latest nudge before it.
    if delivered:
        orders = await _paid_orders(session, list(delivered), since)
        for order in orders:
            paid_at = aware(order.paid_at)
            touches = [s for s in delivered[order.customer_id]
                       if s.sent_at and aware(s.sent_at) <= paid_at <= aware(s.sent_at) + window]
            if not touches:
                continue
            touch = max(touches, key=lambda s: aware(s.sent_at))
            entry = per[str(touch.campaign_id) if touch.campaign_id
                        else f"deleted:{touch.campaign_name}"]
            entry["conversions"] += 1
            entry["revenue"] += order.total or ZERO
            if touch.promo_code and order.promo_code == touch.promo_code:
                entry["coupon_uses"] += 1

    campaigns = []
    totals = {"sent": 0, "failed": 0, "dry_run": 0, "customers": 0, "conversions": 0,
              "revenue": ZERO, "coupon_uses": 0}
    for entry in per.values():
        entry["customers"] = len(entry["customers"])
        entry["conversion_rate"] = (round(entry["conversions"] / entry["customers"], 4)
                                    if entry["customers"] else 0.0)
        for name in totals:
            totals[name] += entry[name]
        entry["revenue"] = f"{entry['revenue']:.2f}"
        campaigns.append(entry)
    totals["conversion_rate"] = (round(totals["conversions"] / totals["customers"], 4)
                                 if totals["customers"] else 0.0)
    totals["revenue"] = f"{totals['revenue']:.2f}"

    opted_out = (await session.execute(
        select(func.count(Customer.id)).where(Customer.nudges_opted_out_at.is_not(None)))
    ).scalar_one()
    return {"days": days, "since": since.isoformat(),
            "attribution_days": settings.attribution_days, "totals": totals,
            "campaigns": sorted(campaigns, key=lambda e: -e["sent"]),
            "customers_opted_out": opted_out}


async def _paid_orders(session, customer_ids: list[uuid.UUID], since: datetime) -> list[Order]:
    found = []
    for start in range(0, len(customer_ids), 500):
        rows = await session.execute(
            select(Order).where(Order.customer_id.in_(customer_ids[start:start + 500]),
                                Order.payment_status == PaymentStatus.PAID,
                                Order.paid_at >= since))
        found.extend(rows.scalars())
    return found
