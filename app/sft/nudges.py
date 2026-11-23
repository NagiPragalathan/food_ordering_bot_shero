"""SFT's follow-ups and the team's tasks, run every few minutes (sft_nudges).

For each lead the journey is not finished with:

  Follow-ups     quiet since their last move: 1st after 24 hours, 2nd after
                 48-72 hours, 3rd after 5-7 days (SFT settings), as approved
                 WhatsApp templates (sft_followup_1..3; the 1st is the payment
                 reminder template for a lead with a payment link). A day after
                 the last one with no answer, the team gets the lead.
  Payment        an hour after the link (in the 24-hour window): the link again.
  Maybe later    a check-in on the day they chose, then the next one.
  Paid           a day later: "have you been able to open the course?"; a day
                 after that with no "Yes, I'm in", the team gets the lead.
  Team tasks     a lead stopped at a stage longer than its time in the drop-off
                 table (attention_hours) is handed to the team, once per stage.

Never to a lead with the team (they asked a person), one who opted out, is
Lost, or has finished. Follow-ups go out only between the settings' hours in
its timezone; a task is raised at any hour.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import SftLead
from app.sft import config, content as c, leads, sending
from app.sft.config import SftConfig
from app.sft.leads import aware, event, now

log = get_logger(__name__)

BATCH = 200


def quiet_hours(cfg: SftConfig, when: datetime | None = None) -> bool:
    local = (when or now()).astimezone(ZoneInfo(cfg.timezone))
    return not cfg.send_from_hour <= local.hour < cfg.send_until_hour


async def run(session: AsyncSession, at: datetime | None = None) -> int:
    """One pass over the open leads; returns how many were acted on."""
    cfg = await config.load(session, fresh=True)
    if not cfg.is_on:
        return 0
    at = at or now()
    open_leads = (await session.execute(
        select(SftLead).where(SftLead.with_team.is_(False), SftLead.opted_out.is_(False),
                              or_(SftLead.stage != c.LOST, SftLead.stage.is_(None)))
        .order_by(SftLead.last_activity_at).limit(BATCH))).scalars().all()
    done = 0
    for lead in open_leads:
        if not cfg.answers(lead.whatsapp_number):
            continue
        try:
            if await one(session, lead, cfg, at):
                done += 1
        except Exception as exc:  # noqa: BLE001 - one lead never stops the rest
            log.error("sft_nudge_failed", lead_id=str(lead.id), error=str(exc))
    if done:
        log.info("sft_nudges_run", acted=done)
    return done


async def one(session: AsyncSession, lead: SftLead, cfg: SftConfig, at: datetime) -> bool:
    if lead.stage == c.ENROLLED:
        return await _after_paying(session, lead, cfg, at)
    acted = await _attention(session, lead, cfg, at)
    if lead.stage == c.PAYMENT_PENDING and await _payment_reminder(session, lead, cfg, at):
        return True
    if lead.nurture_until is not None or lead.step == c.LATER:
        return await _nurture(session, lead, cfg, at) or acted
    return await _followup(session, lead, cfg, at) or acted


# --- follow-ups --------------------------------------------------------------------
async def _followup(session, lead, cfg, at) -> bool:
    sent = lead.followups_sent
    if sent >= len(cfg.followup_hours):
        return await _followups_done(session, lead, cfg, at)
    quiet_since = aware(lead.last_activity_at) or aware(lead.created_at)
    if quiet_since is None or at - quiet_since < timedelta(hours=cfg.followup_hours[sent]):
        return False
    if quiet_hours(cfg, at):
        return False
    spec = c.FOLLOWUPS[min(sent, len(c.FOLLOWUPS) - 1)]
    if sent == 0 and lead.stage == c.PAYMENT_PENDING:
        spec = c.PAYMENT_REMINDER
    how = await sending.template(session, lead, cfg, spec, leads.first(lead), at=at)
    lead.followups_sent = sent + 1
    await event(session, lead, "followup",
                f"Follow-up {sent + 1} of {len(cfg.followup_hours)} ({spec.name})"
                + ("" if how else ": could not be sent"), crm=True,
                data={"n": sent + 1, "template": spec.name, "how": how})
    return True


async def _followups_done(session, lead, cfg, at) -> bool:
    hours = cfg.attention_hours.get("followups_done", 0)
    if not hours:
        return False
    last = await _last(session, lead, "followup")
    if last is None or at - last < timedelta(hours=hours):
        return False
    return await leads.raise_attention(session, lead, "followups_done", hours=hours)


async def _payment_reminder(session, lead, cfg, at) -> bool:
    """The link again, once, an hour after it was sent (inside the window only)."""
    sent = aware(lead.link_sent_at)
    if sent is None or lead.payment_reminded_at is not None or lead.paid_at is not None:
        return False
    if at - sent < timedelta(minutes=cfg.payment_reminder_minutes):
        return False
    if not sending.in_window(lead, at) or quiet_hours(cfg, at):
        return False
    from app.sft.flow import enrol_url

    lead.payment_reminded_at = at
    await sending.link(session, lead, cfg, c.PAY_REMINDER.format(first=leads.first(lead)),
                       url=enrol_url(lead), label=c.PAY_BUTTON.format(fee=cfg.fee_label()),
                       what="payment_reminder")
    await event(session, lead, "followup", "Payment reminder (the link again)", crm=True)
    return True


async def _nurture(session, lead, cfg, at) -> bool:
    """Maybe later: on the day they chose, then cfg.nurture_days after it."""
    if lead.nurture_until is None and lead.step == c.LATER and lead.nurture_sent == 0:
        # Tapped Maybe later but never said when: a week from then.
        lead.nurture_until = (aware(lead.last_activity_at) or at) + timedelta(days=7)
    if lead.nurture_sent > len(cfg.nurture_days) or lead.nurture_until is None:
        return False
    due = aware(lead.nurture_until)
    if due is None or at < due or quiet_hours(cfg, at):
        return False
    how = await sending.template(session, lead, cfg, c.NURTURE_TEMPLATE, leads.first(lead),
                                  at=at)
    lead.nurture_sent += 1
    following = lead.nurture_sent - 1
    lead.nurture_until = (due + timedelta(days=cfg.nurture_days[following])
                          if following < len(cfg.nurture_days) else None)
    await event(session, lead, "followup", f"Maybe later check-in {lead.nurture_sent}"
                + ("" if how else ": could not be sent"), crm=True,
                data={"template": c.NURTURE_TEMPLATE.name, "how": how})
    return True


# --- after paying ------------------------------------------------------------------
async def _after_paying(session, lead, cfg, at) -> bool:
    paid = aware(lead.paid_at)
    if paid is None or lead.access_confirmed_at is not None:
        return False
    checked = aware(lead.access_check_sent_at)
    if checked is None:
        if at - paid < timedelta(hours=cfg.access_check_hours) or quiet_hours(cfg, at):
            return False
        lead.access_check_sent_at = at
        lead.step = c.ACCESS
        how = await sending.template(session, lead, cfg, c.COURSE_CHECK, leads.first(lead),
                                      at=at)
        await event(session, lead, "followup", "Asked if they could open the course"
                    + ("" if how else ": could not be sent"), crm=True,
                    data={"template": c.COURSE_CHECK.name, "how": how})
        return True
    hours = cfg.attention_hours.get("access", 0)
    if hours and at - checked >= timedelta(hours=hours):
        return await leads.raise_attention(session, lead, "access", hours=hours)
    return False


# --- the team's tasks --------------------------------------------------------------
async def _attention(session, lead, cfg, at) -> bool:
    """A lead stopped at its stage too long goes to the team (once per stage)."""
    key = lead.stage
    hours = cfg.attention_hours.get(key, 0)
    if not hours or key not in c.DROPS or key in (lead.attention_raised or []):
        return False
    if lead.needs_attention:
        return False
    since = aware(lead.last_activity_at) or aware(lead.created_at)
    if since is None or at - since < timedelta(hours=hours):
        return False
    return await leads.raise_attention(session, lead, key, hours=hours)


async def _last(session, lead, kind: str) -> datetime | None:
    from app.db.models import SftEvent

    found = (await session.execute(
        select(SftEvent.at).where(SftEvent.lead_id == lead.id, SftEvent.kind == kind)
        .order_by(SftEvent.at.desc()).limit(1))).scalar_one_or_none()
    return aware(found)
