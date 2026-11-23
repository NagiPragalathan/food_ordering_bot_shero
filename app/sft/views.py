"""Where each lead is in SFT's flows, as the admin pages show it: the flow
they are in (the journey, the follow-ups, waiting to pay, maybe later, after
paying, with the team…), what happened last and what the bot does next, and
when. It mirrors app/sft/nudges.py, which does the sending."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SftEvent, SftLead
from app.sft import content as c
from app.sft.config import SftConfig
from app.sft.leads import aware
from app.sft.nudges import followup_due

# The flows, in the order the Nudges page lists them.
FLOWS: dict[str, tuple[str, str]] = {
    "journey": ("In the journey", "The bot is waiting for their answer; follow-up 1 comes if "
                "they go quiet."),
    "followup": ("Follow-ups running", "Quiet after the journey: the automated follow-ups are "
                 "going out."),
    "followups_done": ("Follow-ups finished", "Every follow-up sent with no answer: the team "
                       "gets them."),
    "payment": ("Waiting to pay", "Payment link sent, not paid yet."),
    "nurture": ("Maybe later", "Asked to be contacted later: a check-in on the day they chose."),
    "after_paying": ("Paid: course check", "Enrolled: checking they could open the course."),
    "with_team": ("With the team", "The bot is silent until the team hands the chat back."),
    "done": ("Finished", "Enrolled and in the course, or nothing more to send."),
    "stopped": ("Stopped", "Said Not interested or STOP: nothing more is sent."),
}


@dataclass(frozen=True)
class Flow:
    key: str
    now: str                         # where they are
    next: str = ""                   # what the bot does next
    at: datetime | None = None       # when
    paused: bool = False             # that nudge (or all of them) is switched off

    @property
    def name(self) -> str:
        return FLOWS[self.key][0]


def _gate(cfg: SftConfig, key: str) -> bool:
    return not cfg.nudge_on(key)


def flow_of(lead: SftLead, cfg: SftConfig, last_nudge: datetime | None = None) -> Flow:
    n = len(cfg.followup_hours)
    last_nudge = aware(lead.nudged_at) or aware(last_nudge)
    if lead.with_team:
        return Flow("with_team", "The team is answering them",
                    "Hand back to the bot on the lead's page")
    if lead.opted_out or lead.stage == c.LOST:
        why = f" ({lead.lost_reason})" if lead.lost_reason else ""
        return Flow("stopped", f"Not interested{why}" if lead.stage == c.LOST else "Opted out")
    if lead.stage == c.ENROLLED or lead.paid_at:
        paid = aware(lead.paid_at)
        if lead.access_confirmed_at:
            return Flow("done", "Enrolled and opened the course")
        if lead.access_check_sent_at:
            hours = cfg.attention_hours.get("access", 0)
            return Flow("after_paying", "Asked if they could open the course",
                        "Team task if no answer" if hours else "",
                        aware(lead.access_check_sent_at) + timedelta(hours=hours) if hours else None)
        return Flow("after_paying", "Enrolled", "Course check",
                    paid + timedelta(hours=cfg.access_check_hours) if paid else None,
                    paused=_gate(cfg, "course_check"))
    if lead.stage == c.PAYMENT_PENDING and lead.link_sent_at and not lead.payment_reminded_at:
        return Flow("payment", "Payment link sent", "Payment link again",
                    aware(lead.link_sent_at) + timedelta(minutes=cfg.payment_reminder_minutes),
                    paused=_gate(cfg, "payment_reminder"))
    if lead.nurture_until is not None or lead.step == c.LATER:
        if lead.nurture_until is None:
            return Flow("nurture", "Tapped Maybe later", "Check-in a week after, unless they "
                        "choose a day", paused=_gate(cfg, "nurture"))
        return Flow("nurture", f"Maybe later ({lead.nurture_sent} check-in"
                    f"{'s' if lead.nurture_sent != 1 else ''} sent)", "Check-in",
                    aware(lead.nurture_until), paused=_gate(cfg, "nurture"))
    sent = lead.followups_sent
    if sent >= n:
        hours = cfg.attention_hours.get("followups_done", 0)
        return Flow("followups_done", f"All {n} follow-ups sent, no answer",
                    "Team task" if hours and not lead.needs_attention else "",
                    aware(last_nudge) + timedelta(hours=hours) if hours and last_nudge
                    and not lead.needs_attention else None)
    due = followup_due(lead, cfg, last_nudge)
    key = f"followup_{sent + 1}"
    if lead.stage == c.PAYMENT_PENDING:
        now_text = "Payment link sent and reminded"
        flow = "payment"
    elif sent == 0:
        waiting = c.STEP_LABELS.get(lead.step) or ""
        now_text = f"Waiting for {waiting}" if waiting else "In the journey"
        flow = "journey"
    else:
        now_text = f"Follow-up {sent} of {n} sent"
        flow = "followup"
    return Flow(flow, now_text, f"Follow-up {sent + 1}", due, paused=_gate(cfg, key))


async def last_nudges(session: AsyncSession, ids) -> dict:
    """Each lead's last automated nudge (follow-up, reminder, check-in)."""
    ids = list(ids)
    if not ids:
        return {}
    rows = await session.execute(
        select(SftEvent.lead_id, func.max(SftEvent.at))
        .where(SftEvent.lead_id.in_(ids), SftEvent.kind == "followup")
        .group_by(SftEvent.lead_id))
    return {lead_id: at for lead_id, at in rows.all()}


def course_state(lead: SftLead) -> tuple[str, str]:
    """A student's course access: (label, badge tone)."""
    if lead.refunded_at:
        return "Refunded", "bad"
    if lead.access_confirmed_at:
        return "Opened the course", "good"
    if lead.needs_attention and lead.attention_stage == "course_link":
        return "Team to send access", "warn"
    if lead.with_team or (lead.needs_attention and lead.attention_stage == "access_help"):
        return "Needs help", "bad"
    if lead.access_check_sent_at:
        return "Asked, no answer yet", "warn"
    return "Not asked yet", "default"


def payment_state(lead: SftLead) -> tuple[str, str]:
    if lead.refunded_at:
        return "Refunded", "bad"
    if lead.test_payment:
        return "Test (not charged)", "warn"
    if not lead.payment_intent_id:
        return "Marked paid by the team", "info"
    return "Paid on Stripe", "good"
