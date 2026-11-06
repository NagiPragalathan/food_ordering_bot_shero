"""Answers to a nudge: the buttons Later and No, typed or tapped, and STOP.

Later and No put the customer in that bucket, whose sequence takes over
(services/nudges/runner.py), and get the bucket's reply. STOP ends the nudges
for good. Order Food is the bot's ORDER FOOD keyword, so it starts an order
as usual; here it only marks the nudge as answered and ends any bucket.

Only from someone a nudge wrote to lately, and not part-way through an order,
where "no" answers the bot's own question.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import ConversationStep, Customer, NudgeBucket, NudgeLead, NudgeSend
from app.services.customers import get_customer
from app.services.nudges import config, crm
from app.services.nudges.runner import IDLE_STEPS, aware, stage_entered

log = get_logger(__name__)

ANSWER_DAYS = 14        # a nudge older than this is not what they are answering
WORDS = {"later": NudgeBucket.LATER, "no": NudgeBucket.NO, "stop": NudgeBucket.DEAD,
         "order food": "order"}
LABELS = {NudgeBucket.LATER: "Later", NudgeBucket.NO: "No", NudgeBucket.DEAD: "STOP",
          "order": "Order Food"}
STOP_REPLY = "Done - you won't get these messages from us any more."


def word(text: str | None) -> str | None:
    clean = " ".join((text or "").lower().replace("-", " ").split()).strip(" .!")
    return WORDS.get(clean)


async def last_nudge(session: AsyncSession, customer: Customer,
                     now: datetime | None = None) -> NudgeSend | None:
    since = (now or datetime.now(timezone.utc)) - timedelta(days=ANSWER_DAYS)
    return (await session.execute(
        select(NudgeSend).where(NudgeSend.customer_id == customer.id, NudgeSend.status == "sent",
                                NudgeSend.sent_at >= since)
        .order_by(NudgeSend.sent_at.desc()).limit(1))).scalar_one_or_none()


async def is_reply(session: AsyncSession, number: str, message: str) -> bool:
    """For services/reply_triggers: a typed Later / No / STOP answering a nudge
    is answered even when the bot otherwise waits for a trigger keyword."""
    which = word(message)
    if which is None or which == "order":
        return False
    customer = await get_customer(session, number)
    return customer is not None and await last_nudge(session, customer) is not None


def _mid_order(conversation, now: datetime) -> bool:
    if conversation is None or conversation.step in {s.value for s in IDLE_STEPS}:
        return False
    last = aware(conversation.last_message_at)
    return last is not None and now - last <= timedelta(hours=24)


async def handle(ctx) -> bool:
    """From the conversation engine. True if the message was a nudge answer
    that needs nothing more; Order Food carries on into the order."""
    which = word(ctx.event.reply_title or ctx.event.text or ctx.choice)
    if which is None:
        return False
    now = datetime.now(timezone.utc)
    if ctx.conversation.step == ConversationStep.HANDED_OVER or _mid_order(ctx.conversation, now):
        return False
    send = await last_nudge(ctx.session, ctx.customer, now)
    if send is None:
        return False

    customer, session = ctx.customer, ctx.session
    lead = (await session.execute(select(NudgeLead).where(
        NudgeLead.customer_id == customer.id))).scalar_one_or_none()
    if lead is None:
        lead = NudgeLead(customer_id=customer.id, stage=customer.lead_stage,
                         stage_entered_at=stage_entered(customer), attempts=0, failures=0)
        session.add(lead)
    label = LABELS[which]
    send.reply, send.replied_at = label, now
    await crm.message_replied(send)            # its Nudge Messages record, if filed
    lead.last_reply, lead.last_reply_at = label, now
    cfg = await config.load(session)
    lines = [f"Tapped {label} on {send.template}."]

    if which == "order":
        # They are ordering: out of any bucket, and the stage's message counts as sent.
        if lead.bucket:
            lines.append(f"Out of {crm.LABELS.get(lead.bucket, lead.bucket)}.")
        lead.bucket = lead.bucket_from = lead.bucket_at = None
        lead.stage_sent_at, lead.next_due_at, lead.waiting = now, None, None
        lead.attempts = lead.failures = 0
        await session.flush()
        if cfg.zoho_sync:
            await crm.sync(customer, lead, lines)
        return False

    if which == NudgeBucket.DEAD:
        customer.nudges_opted_out_at = now
        lead.bucket, lead.next_due_at, lead.waiting = NudgeBucket.DEAD, None, "Replied STOP"
        reply = STOP_REPLY
        lines.append("Opted out: no more nudges.")
    else:
        lead.bucket, lead.bucket_from, lead.bucket_at = which, lead.stage, now
        lead.attempts = lead.failures = 0
        seq = cfg.sequences[which]
        delay = seq.delay(0)
        lead.next_due_at = now + delay if seq.active and delay else None
        lead.waiting = None
        # Something must answer the tap, or the bot's "didn't get that" would.
        reply = seq.reply or "Got it - thanks for letting us know."
        lines.append(f"Moved to {LABELS[which]}.")
    await session.flush()
    log.info("nudge_reply", customer_id=str(customer.id), reply=label, template=send.template)
    await ctx.reply_text(reply)
    if cfg.zoho_sync:
        await crm.sync(customer, lead, lines)
    return True
