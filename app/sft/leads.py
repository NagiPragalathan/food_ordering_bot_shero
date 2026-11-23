"""A lead's state: creating one, its timeline, its stage, and the team's tasks.

Every change marks the lead for the Zoho sync (crm_dirty); the events that
mean something to a salesperson (a stage, a question, a payment, a task)
also go to Zoho as notes (crm_pending).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import SftEvent, SftLead
from app.sft import content as c

log = get_logger(__name__)

DETAIL_MAX = 600


def now() -> datetime:
    return datetime.now(timezone.utc)


def aware(when: datetime | None) -> datetime | None:
    """SQLite hands datetimes back without a timezone; they are UTC."""
    if when is None:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def digits(number: str) -> str:
    return "".join(ch for ch in (number or "") if ch.isdigit())


async def get(session: AsyncSession, number: str) -> SftLead | None:
    number = digits(number)
    if not number:
        return None
    return (await session.execute(
        select(SftLead).where(SftLead.whatsapp_number == number))).scalar_one_or_none()


async def by_token(session: AsyncSession, token: str) -> SftLead | None:
    if not token or len(token) > 32:
        return None
    return (await session.execute(
        select(SftLead).where(SftLead.pay_token == token))).scalar_one_or_none()


async def create(session: AsyncSession, number: str, *, profile_name: str | None = None,
                 source: str = "WhatsApp", source_detail: str | None = None,
                 ad_id: str | None = None, campaign_id: str | None = None,
                 first_message: str | None = None, channel_id: str | None = None) -> SftLead:
    lead = SftLead(
        whatsapp_number=digits(number), profile_name=(profile_name or "").strip()[:120] or None,
        source=source[:60], source_detail=(source_detail or None) and source_detail[:200],
        ad_id=ad_id or None, campaign_id=campaign_id or None,
        first_message=(first_message or "").strip()[:300] or None,
        channel_id=channel_id or None, stage=c.NEW, step=c.IDLE,
        stage_times={c.NEW: now().isoformat()}, pay_token=secrets.token_urlsafe(12)[:16],
        last_activity_at=now(), crm_dirty=True, attention_raised=[], zoho_tags=[],
    )
    session.add(lead)
    await session.flush()
    await event(session, lead, "stage", f"New lead from {source}"
                + (f" ({source_detail})" if source_detail else ""), crm=True,
                data={"to": c.NEW, "source": source})
    log.info("sft_lead_created", lead_id=str(lead.id), source=source,
             number_tail=lead.whatsapp_number[-4:])
    return lead


_last_stamp: list[datetime] = []


def _stamp() -> datetime:
    """Now, and always later than the event before it: some clocks (Windows)
    tick too coarsely to keep a reply after the message it answers."""
    at = now()
    if _last_stamp and at <= _last_stamp[0] and _last_stamp[0] - at < timedelta(seconds=5):
        at = _last_stamp[0] + timedelta(microseconds=1)
    _last_stamp[:] = [at]
    return at


async def event(session: AsyncSession, lead: SftLead, kind: str, detail: str = "", *,
                crm: bool = False, data: dict | None = None) -> SftEvent:
    row = SftEvent(lead_id=lead.id, at=_stamp(), kind=kind, stage=lead.stage,
                   detail=(detail or "")[:DETAIL_MAX], data=data or {}, crm_pending=crm)
    session.add(row)
    if crm:
        lead.crm_dirty = True
    return row


def touch(lead: SftLead) -> None:
    """They moved: the follow-up clock starts again from now."""
    lead.last_activity_at = now()


async def move(session: AsyncSession, lead: SftLead, stage: str, why: str = "") -> bool:
    """Put the lead in `stage`; False when it already is. A stage reached
    for the first time is stamped for the funnel; an open team task for an
    earlier stage is closed, since the lead moved on."""
    if lead.stage == stage:
        return False
    before = lead.stage
    lead.stage = stage
    times = dict(lead.stage_times or {})
    times.setdefault(stage, now().isoformat())
    lead.stage_times = times
    lead.crm_dirty = True
    if lead.needs_attention and lead.attention_stage not in ("question", "access_help",
                                                             "course_link"):
        await resolve(session, lead, "moved on by themselves", by="the bot")
    await event(session, lead, "stage",
                f"{c.STAGES.get(before, before)} → {c.STAGES.get(stage, stage)}"
                + (f": {why}" if why else ""), crm=True, data={"from": before, "to": stage})
    return True


def reached(lead: SftLead, stage: str) -> bool:
    return stage in (lead.stage_times or {})


async def raise_attention(session: AsyncSession, lead: SftLead, key: str, *,
                          hours: float | None = None, detail: str = "") -> bool:
    """Give the lead to the team for drop-off `key` (content.DROPS), once per key."""
    drop = c.DROPS[key]
    raised = list(lead.attention_raised or [])
    if key in raised:
        return False
    raised.append(key)
    lead.attention_raised = raised
    lead.needs_attention = True
    lead.attention_stage = key
    lead.attention_priority = drop.priority
    lead.attention_since = now()
    reason = drop.trigger.format(hours=_hours(hours))
    lead.attention_reason = reason[:200]
    await event(session, lead, "attention", f"For the team ({drop.stage_name}): {reason}"
                + (f". {detail}" if detail else ""), crm=True,
                data={"key": key, "priority": drop.priority, "task": True})
    log.info("sft_attention_raised", lead_id=str(lead.id), key=key, priority=drop.priority)
    return True


async def resolve(session: AsyncSession, lead: SftLead, outcome: str, *, by: str) -> None:
    if not lead.needs_attention:
        return
    since = aware(lead.attention_since)
    minutes = int((now() - since).total_seconds() // 60) if since else None
    lead.needs_attention = False
    await event(session, lead, "resolved", f"Team task closed by {by}: {outcome}", crm=True,
                data={"key": lead.attention_stage, "minutes": minutes, "by": by})
    lead.attention_stage = None
    lead.attention_reason = None
    lead.attention_priority = None
    lead.attention_since = None


def _hours(hours: float | None) -> str:
    if hours is None:
        return "?"
    return str(int(hours)) if float(hours).is_integer() else f"{hours:g}"


def first(lead: SftLead) -> str:
    return c.first_name(lead.name or lead.profile_name)


def label(lead: SftLead) -> str:
    """How the team sees the lead: their name and the end of their number."""
    return f"{lead.name or lead.profile_name or 'Unknown'} (***{lead.whatsapp_number[-4:]})"
