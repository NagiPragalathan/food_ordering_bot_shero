"""The send log and what the nudges earned.

A conversion is a paid order within `attribution_days` of a nudge, credited
to the latest nudge before it (last touch) - so two campaigns reaching the
same customer never both claim one order.

Follow-up sends also count per library message (`messages`): sends, replies
(the customer wrote back within REPLY_HOURS) and the orders credited to it.
"""

from __future__ import annotations

import bisect
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Customer, InboundMessage, NudgeSend, NudgeStatus, Order, PaymentStatus
from app.services.nudges import store
from app.services.nudges.audience import aware, digits

ZERO = Decimal("0.00")
MAX_LIMIT = 500
# A message from the customer this soon after a follow-up counts as a reply to it.
REPLY_HOURS = 48


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
        "message_code": row.message_code,
        "note": row.note,
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
    by_message: dict[str, dict] = {}
    delivered: dict[uuid.UUID, list[NudgeSend]] = defaultdict(list)
    for row in sends:
        if row.message_code and row.status in (NudgeStatus.SENT, NudgeStatus.FAILED):
            entry = by_message.setdefault(row.message_code, {
                "code": row.message_code, "sent": 0, "failed": 0, "customers": set(),
                "replies": 0, "keywords": 0, "delivered": 0, "read": 0, "conversions": 0,
                "revenue": ZERO})
            if row.status == NudgeStatus.FAILED or row.delivery_status == "failed":
                entry["failed"] += 1
            if row.status == NudgeStatus.SENT:
                entry["sent"] += 1
                entry["customers"].add(row.customer_id)
                entry["keywords"] += int(row.keyword_at is not None)
                entry["delivered"] += int(row.delivery_status in ("delivered", "read"))
                entry["read"] += int(row.delivery_status == "read")
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
            if touch.message_code in by_message:
                by_message[touch.message_code]["conversions"] += 1
                by_message[touch.message_code]["revenue"] += order.total or ZERO

    if by_message:
        await _count_replies(session, [r for r in sends if r.message_code in by_message
                                       and r.status == NudgeStatus.SENT], by_message, since)

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
    messages = []
    for entry in by_message.values():
        entry["customers"] = len(entry["customers"])
        entry["reply_rate"] = round(entry["replies"] / entry["sent"], 4) if entry["sent"] else 0.0
        entry["keyword_rate"] = round(entry["keywords"] / entry["sent"], 4) if entry["sent"] else 0.0
        entry["average_order"] = (f"{entry['revenue'] / entry['conversions']:.2f}"
                                  if entry["conversions"] else None)
        entry["revenue"] = f"{entry['revenue']:.2f}"
        messages.append(entry)
    return {"days": days, "since": since.isoformat(),
            "attribution_days": settings.attribution_days, "totals": totals,
            "campaigns": sorted(campaigns, key=lambda e: -e["sent"]),
            "messages": sorted(messages, key=lambda e: e["code"]),
            "customers_opted_out": opted_out}


async def _count_replies(session, sends: list[NudgeSend], by_message: dict,
                         since: datetime) -> None:
    """A send counts as replied to when the customer wrote within REPLY_HOURS."""
    numbers = {digits(r.whatsapp_number) for r in sends}
    rows = await session.execute(
        select(InboundMessage.whatsapp_number, InboundMessage.created_at)
        .where(InboundMessage.created_at >= since))
    inbound: dict[str, list[datetime]] = defaultdict(list)
    for number, at in rows:
        number = digits(number)
        if number in numbers:
            inbound[number].append(aware(at))
    for times in inbound.values():
        times.sort()
    window = timedelta(hours=REPLY_HOURS)
    for row in sends:
        times, sent_at = inbound.get(digits(row.whatsapp_number), []), aware(row.sent_at)
        if not times or sent_at is None:
            continue
        i = bisect.bisect_right(times, sent_at)
        if i < len(times) and times[i] <= sent_at + window:
            by_message[row.message_code]["replies"] += 1


async def _paid_orders(session, customer_ids: list[uuid.UUID], since: datetime) -> list[Order]:
    found = []
    for start in range(0, len(customer_ids), 500):
        rows = await session.execute(
            select(Order).where(Order.customer_id.in_(customer_ids[start:start + 500]),
                                Order.payment_status == PaymentStatus.PAID,
                                Order.paid_at >= since))
        found.extend(rows.scalars())
    return found


# --- the SMO dashboard (handoff section 11) ---------------------------------------------------
async def smo_dashboard(session: AsyncSession, *, days: int = 30,
                        now: datetime | None = None) -> dict:
    """What the handoff's dashboard asks for, beyond the per-message results
    in `stats`: who is where, what was held back and why, the ordering flow,
    Dead Leads, and the suppressions and failures."""
    from app.db.models import NudgeJourney, NudgeKind, NudgeSkip
    from app.services.nudges import followup

    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    summary = await followup.summary(session)
    journeys = {"total": 0, "due_now": 0, "by_crm_bucket": defaultdict(int),
                "by_stage": defaultdict(int), "by_bucket": defaultdict(int),
                "end_reasons": defaultdict(int), "flow": defaultdict(int)}
    dead_days = []
    for entry in summary.values():
        journeys["total"] += entry["total"]
        journeys["due_now"] += entry["due_now"]
        for name in ("by_crm_bucket", "by_stage", "by_bucket", "end_reasons", "flow"):
            for k, v in entry[name].items():
                journeys[name][str(k)] += v
        if entry.get("dead_avg_days") is not None:
            dead_days.append(entry["dead_avg_days"])
    journeys = {k: dict(v) if isinstance(v, defaultdict) else v for k, v in journeys.items()}
    journeys["dead_avg_days"] = round(sum(dead_days) / len(dead_days), 1) if dead_days else None

    deferred, passed = defaultdict(int), defaultdict(lambda: defaultdict(int))
    rows = await session.execute(
        select(NudgeSkip.reason, NudgeSkip.message_code, func.sum(NudgeSkip.count))
        .where(NudgeSkip.day >= since.date())
        .group_by(NudgeSkip.reason, NudgeSkip.message_code))
    for reason, code, count in rows:
        if code:
            passed[code][reason] += int(count or 0)
        else:
            deferred[reason] += int(count or 0)

    sends = (await session.execute(
        select(NudgeSend.status, NudgeSend.delivery_status, NudgeSend.zoho_error)
        .where(NudgeSend.kind == NudgeKind.FOLLOW_UP, NudgeSend.created_at >= since))).all()
    journey_zoho = (await session.execute(
        select(func.count(NudgeJourney.id)).where(NudgeJourney.zoho_error.is_not(None)))
    ).scalar_one()
    holds = defaultdict(int)
    for hold, count in await session.execute(
            select(Customer.nudge_hold, func.count(Customer.id))
            .where(Customer.nudge_hold.is_not(None)).group_by(Customer.nudge_hold)):
        holds[hold] += count
    opted_out = (await session.execute(
        select(func.count(Customer.id)).where(Customer.nudges_opted_out_at.is_not(None)))
    ).scalar_one()
    return {
        "days": days,
        "journeys": journeys,
        "deferred": dict(sorted(deferred.items(), key=lambda kv: -kv[1])),
        "passed_over": {code: dict(reasons) for code, reasons in sorted(passed.items())},
        "sends": {
            "sent": sum(1 for st, _, _ in sends if st == NudgeStatus.SENT),
            "failed": sum(1 for st, d, _ in sends if st == NudgeStatus.FAILED or d == "failed"),
            "delivered": sum(1 for _, d, _ in sends if d in ("delivered", "read")),
            "read": sum(1 for _, d, _ in sends if d == "read"),
            "zoho_failed": sum(1 for st, _, z in sends if st == NudgeStatus.SENT and z
                               and not z.startswith("Noted")),
        },
        "zoho_field_failures": journey_zoho,
        "suppressed": {"opted_out": opted_out, "holds": dict(holds)},
    }
