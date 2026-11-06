"""The nudges' run, every five minutes (workers/jobs.py).

For each customer in a stage with a rule turned on:

  * after `wait_hours` in the stage, send the rule's message - once for each
    time they enter the stage;
  * if they are still in the stage `no_response_hours` after it, move them to
    the No response bucket.

For each customer in a bucket (No response, Later, No) whose sequence is on:
send its next message when due, and make them a Dead Lead when it ends.

Moving to another stage means they are ordering again: any bucket ends and
the new stage's rule applies. Paying ends it all. Messages only go inside the
send hours, never while the customer is chatting with the bot or the team,
and never once they replied STOP; moves to a bucket happen at any hour.
Before each send the customer's stage in Zoho is read, and the team's
decision there wins (services/nudges/crm.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConfigurationError, IntegrationError
from app.core.logging import get_logger
from app.db.models import (Conversation, ConversationStep, Customer, LeadStage, NudgeBucket,
                           NudgeLead, NudgeSend)
from app.integrations.gallabox.sender import current_sender
from app.services.nudges import config, crm, messages
from app.services.nudges.config import BUCKETS, NudgeSettings

log = get_logger(__name__)

# A stage entered longer ago than its wait plus this is left alone, so turning
# a rule on does not write to everyone who stopped there months ago.
LOOKBACK = timedelta(days=7)
# Chatting with the bot this recently, mid-order: the message waits.
CHATTING = timedelta(hours=1)
IDLE_STEPS = {ConversationStep.START, ConversationStep.COMPLETED, ConversationStep.MAIN_MENU}
MAX_FAILURES = 3        # a message that fails this often in a row is given up
MAX_SENDS_PER_RUN = 300


def aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)   # SQLite: naive UTC


def stage_entered(customer: Customer) -> datetime | None:
    stamp = (customer.stage_timestamps or {}).get(customer.lead_stage)
    try:
        return aware(datetime.fromisoformat(stamp)) if stamp else None
    except ValueError:
        return None


def in_send_hours(cfg: NudgeSettings, now: datetime) -> bool:
    hour = now.astimezone(ZoneInfo(cfg.timezone)).hour
    return cfg.send_from <= hour < cfg.send_to


def chatting(conversation: Conversation | None, now: datetime) -> str | None:
    """Why a message must wait for this conversation, if it must."""
    if conversation is None:
        return None
    if conversation.step == ConversationStep.HANDED_OVER:
        return "Chat is with your team"
    last = aware(conversation.last_message_at)
    if conversation.step not in {s.value for s in IDLE_STEPS} and last and now - last < CHATTING:
        return "Ordering with the bot right now"
    return None


class Run:
    """One pass over everyone. Counts what it did, for the log and the page."""

    def __init__(self, session: AsyncSession, cfg: NudgeSettings, now: datetime, *,
                 file_messages: bool = False):
        self.session, self.cfg, self.now = session, cfg, now
        self.in_hours = in_send_hours(cfg, now)
        self.counts = {"sent": 0, "failed": 0, "moved": 0, "dead": 0}
        # Each message also gets a record in Zoho's Nudge Messages module.
        self.file_messages = file_messages

    # -- one customer --------------------------------------------------------------
    async def customer(self, customer: Customer, lead: NudgeLead | None,
                       conversation: Conversation | None) -> None:
        lines: list[str] = []
        template = None
        stage, entered = customer.lead_stage, stage_entered(customer)

        if lead is not None and (lead.stage != stage or (
                entered and lead.stage_entered_at and entered > aware(lead.stage_entered_at))):
            if lead.bucket:
                lines.append(f"Back to ordering ({stage}): out of {_bucket_name(lead.bucket)}.")
            _restart(lead, stage, entered)

        if stage == LeadStage.CONVERTED:
            if lead is not None and (lead.bucket or lead.next_due_at):
                lead.bucket, lead.next_due_at, lead.waiting = None, None, None
                lines.append("Ordered: nudges stopped.")
        elif customer.nudges_opted_out_at is not None:
            if lead is not None:
                lead.next_due_at, lead.waiting = None, "Replied STOP"
        elif lead is not None and lead.bucket:
            template = await self._bucket(customer, lead, conversation, lines)
        else:
            lead, template = await self._stage(customer, lead, conversation, entered, lines)

        if lead is not None and (lines or template) and self.cfg.zoho_sync:
            await crm.sync(customer, lead, lines, template=template)

    async def _stage(self, customer, lead, conversation, entered, lines):
        rule = self.cfg.rule(customer.lead_stage)
        if rule is None or not rule.active or entered is None:
            if lead is not None:
                lead.next_due_at, lead.waiting = None, "No rule on for this stage"
            return lead, None
        due = entered + timedelta(hours=rule.wait_hours)
        if lead is None or lead.stage_sent_at is None:
            if self.now > due + LOOKBACK:
                return lead, None              # stopped here long before the rule was on
            if lead is None:
                lead = NudgeLead(customer_id=customer.id, stage=customer.lead_stage,
                                 stage_entered_at=entered, attempts=0, failures=0)
                self.session.add(lead)
            lead.next_due_at, lead.waiting = due, None
            if self.now < due:
                return lead, None
            if await self._send(customer, lead, conversation, rule.message, kind="stage", attempt=1):
                lead.stage_sent_at = self.now
                lead.next_due_at = (self.now + timedelta(hours=rule.no_response_hours)
                                    if rule.no_response_hours else None)
                lines.append(f"Sent {rule.message} ({customer.lead_stage}, after "
                             f"{rule.wait_hours}h in the stage).")
                return lead, rule.message
            return lead, None
        if not rule.no_response_hours:
            lead.next_due_at = None
            return lead, None
        due = aware(lead.stage_sent_at) + timedelta(hours=rule.no_response_hours)
        lead.next_due_at = due
        if self.now >= due:
            _into_bucket(lead, NudgeBucket.NO_RESPONSE, self.now)
            lead.next_due_at = self._next_due(lead)
            self.counts["moved"] += 1
            lines.append(f"No answer {rule.no_response_hours}h after the {customer.lead_stage} "
                         f"message: moved to No response.")
        return lead, None

    async def _bucket(self, customer, lead, conversation, lines) -> str | None:
        if lead.bucket == NudgeBucket.DEAD:
            lead.next_due_at = None
            return None
        seq = self.cfg.sequences[lead.bucket]
        if not seq.active:
            lead.next_due_at, lead.waiting = None, f"{_bucket_name(lead.bucket)} sequence is off"
            return None
        started = aware(lead.bucket_at) or self.now
        if seq.dead_after_days and self.now >= started + timedelta(days=seq.dead_after_days):
            return self._dead(lead, lines, f"{seq.dead_after_days} days in {_bucket_name(lead.bucket)}")
        if seq.dead_after_messages and lead.attempts >= seq.dead_after_messages:
            return self._dead(lead, lines, f"{lead.attempts} message(s) sent")
        due = self._next_due(lead)
        if due is None:
            return self._dead(lead, lines, "the sequence ran its full length")
        lead.next_due_at = due
        if self.now < due:
            return None
        name = seq.message_for(lead.attempts)
        if await self._send(customer, lead, conversation, name, kind=lead.bucket,
                            attempt=lead.attempts + 1):
            lead.attempts += 1
            lead.last_sent_at = self.now
            lead.next_due_at = self._next_due(lead)
            lines.append(f"Sent {name} ({_bucket_name(lead.bucket)}, message {lead.attempts}).")
            return name
        return None

    def _next_due(self, lead: NudgeLead) -> datetime | None:
        seq = self.cfg.sequences.get(lead.bucket or "")
        if seq is None:
            return None
        delay = seq.delay(lead.attempts)
        if delay is None:
            return None
        start = aware(lead.last_sent_at) if lead.attempts else aware(lead.bucket_at)
        return (start or self.now) + delay

    def _dead(self, lead: NudgeLead, lines: list[str], why: str) -> None:
        lead.bucket, lead.next_due_at, lead.waiting = NudgeBucket.DEAD, None, None
        self.counts["dead"] += 1
        lines.append(f"Dead Lead: {why}.")
        return None

    # -- sending ---------------------------------------------------------------------
    async def _send(self, customer, lead, conversation, name: str | None, *, kind: str,
                    attempt: int) -> bool:
        """Send `name`. True when the step is done: sent, or failed too often."""
        message = self.cfg.message(name)
        if message is None:
            lead.waiting = "No message chosen"
            return False
        if not self.in_hours:
            lead.waiting = f"Waits for the send hours ({self.cfg.send_from}:00-{self.cfg.send_to}:00)"
            return False
        if sum(self.counts[k] for k in ("sent", "failed")) >= MAX_SENDS_PER_RUN:
            lead.waiting = "Next run (this run sent its limit)"
            return False
        why = chatting(conversation, self.now)
        if why:
            lead.waiting = why
            return False
        if not await messages.approved(message.name):
            lead.waiting = f"{message.name} is not approved by WhatsApp yet"
            return False
        # Zoho first: the team's decision there (Converted, closed, a bucket)
        # wins over what the bot was about to send.
        if self.cfg.zoho_sync:
            why = await crm.follow(customer, lead, self.now)
            if why:
                lead.waiting = why
                return False
        send = NudgeSend(customer_id=customer.id, kind=kind, stage=lead.stage, attempt=attempt,
                         template=message.name, sent_at=self.now)
        try:
            result = await current_sender().send_template_only(
                customer.whatsapp_number, messages.spec(message), *messages.values(message, customer))
        except (IntegrationError, ConfigurationError, ValueError) as exc:
            send.status, send.error = "failed", str(exc)[:500]
            self.session.add(send)
            if self.file_messages:
                await crm.file_message(customer, send)
            self.counts["failed"] += 1
            lead.failures += 1
            log.warning("nudge_send_failed", customer_id=str(customer.id), template=message.name,
                        error=str(exc))
            if lead.failures >= MAX_FAILURES:
                lead.failures, lead.waiting = 0, f"Gave up after {MAX_FAILURES} failed sends: {exc}"[:200]
                return True
            lead.waiting = f"Send failed, will retry: {exc}"[:200]
            return False
        send.status, send.provider_message_id = "sent", provider_id(result)
        self.session.add(send)
        if self.file_messages:
            await crm.file_message(customer, send)
        self.counts["sent"] += 1
        lead.failures, lead.waiting, lead.last_sent_at = 0, None, self.now
        log.info("nudge_sent", customer_id=str(customer.id), template=message.name, kind=kind)
        return True


def _bucket_name(bucket: str) -> str:
    return BUCKETS.get(bucket) or ("Dead Lead" if bucket == NudgeBucket.DEAD else bucket)


def _restart(lead: NudgeLead, stage: str, entered: datetime | None) -> None:
    """A new stage: they are ordering again."""
    lead.stage, lead.stage_entered_at, lead.stage_sent_at = stage, entered, None
    lead.bucket = lead.bucket_from = lead.bucket_at = None
    lead.attempts = lead.failures = 0
    lead.next_due_at = lead.waiting = None


def _into_bucket(lead: NudgeLead, bucket: str, now: datetime) -> None:
    lead.bucket, lead.bucket_from, lead.bucket_at = bucket, lead.stage, now
    lead.attempts = lead.failures = 0
    lead.waiting = None


def provider_id(result) -> str | None:
    """The message id in Gallabox's answer to a send."""
    if not isinstance(result, dict):
        return None
    for source in (result, result.get("data") or {}, result.get("message") or {}):
        if isinstance(source, dict):
            for name in ("id", "messageId", "message_id", "_id"):
                if source.get(name):
                    return str(source[name])[:120]
    return None


async def run(session: AsyncSession, now: datetime | None = None) -> dict:
    """One pass. Nothing is sent while the nudges are switched off; Zoho is
    still set up for them (once a day), so it is ready when they are on."""
    cfg = await config.load(session)
    zoho = await crm.ensure(session) if cfg.zoho_sync else {}
    if not cfg.enabled:
        return {"off": True}
    now = now or datetime.now(timezone.utc)
    job = Run(session, cfg, now, file_messages=bool(zoho.get("module_ready")))
    active_stages = [r.stage for r in cfg.rules if r.active]

    leads = {lead.customer_id: lead for lead in (await session.execute(select(NudgeLead))).scalars()}
    query = select(Customer).where(or_(
        Customer.id.in_(list(leads)) if leads else false(),
        Customer.lead_stage.in_(active_stages) if active_stages else false()))
    customers = list((await session.execute(query)).scalars())
    conversations = {c.customer_id: c for c in (await session.execute(select(Conversation).where(
        Conversation.customer_id.in_([c.id for c in customers])))).scalars()} if customers else {}

    for customer in customers:
        try:
            await job.customer(customer, leads.get(customer.id), conversations.get(customer.id))
        except Exception as exc:  # noqa: BLE001 - one customer never stops the rest
            log.error("nudge_customer_failed", customer_id=str(customer.id), error=str(exc))
    await session.flush()
    if any(job.counts.values()):
        log.info("nudges_run", **job.counts)
    return job.counts
