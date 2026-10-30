"""STOP and START OFFERS: a customer's own say over nudges - and the team's hold.

Honoured whatever the master switch says - someone who asks to stop is
stopped, even if nudges are off today and turned on next month. Only nudges
stop: order updates, payment links and receipts still reach them.

The keywords and replies are in the nudge settings. A message has to be the
keyword and nothing else, so "stop the order" is not an opt-out.

A hold is the team's: an unresolved complaint, a support issue, a number that
cannot be reached (handoff sections 7 and 9). While it is set no nudge goes,
and it stays until the team lifts it. Either way the customer's Zoho record
shows it (Suppression_Status, Retargeting_Status).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConfigurationError, IntegrationError
from app.core.logging import get_logger
from app.db.models import Customer
from app.integrations.zoho import engagement
from app.services.nudges import crm_fields, store
from app.services.nudges.config import NudgeConfigError
from app.services.nudges.runner import zoho_record

log = get_logger(__name__)


def _normal(text: str) -> str:
    return " ".join((text or "").lower().replace("-", " ").split())


async def keyword(session: AsyncSession, message: str) -> str | None:
    """"stop" or "start" if the message is one of the keywords, else None.

    Also asked by services/reply_triggers, so STOP is answered even when the
    bot otherwise replies to trigger keywords only.
    """
    text = _normal(message)
    if not text or len(text) > 40:
        return None
    _, settings = await store.load_settings(session)
    if text in settings.stop_keywords:
        return "stop"
    if text in settings.start_keywords:
        return "start"
    return None


async def handle_keyword(ctx) -> bool:
    """Act on STOP / START OFFERS from the conversation engine. True if it was one."""
    which = await keyword(ctx.session, ctx.choice)
    if which is None:
        return False
    _, settings = await store.load_settings(ctx.session)
    if which == "stop":
        await opt_out(ctx.session, ctx.customer, source="whatsapp")
        await ctx.reply_text(settings.opt_out_reply)
    else:
        await opt_in(ctx.session, ctx.customer, source="whatsapp")
        await ctx.reply_text(settings.opt_in_reply)
    return True


async def opt_out(session: AsyncSession, customer: Customer, *, source: str) -> bool:
    """Stop nudges to this customer. True if they were not already opted out."""
    if customer.nudges_opted_out_at is not None:
        return False
    customer.nudges_opted_out_at = datetime.now(timezone.utc)
    await session.flush()
    log.info("nudges_opted_out", customer_id=str(customer.id), source=source)
    await _tell_zoho(session, customer, opted_out=True, source=source)
    await crm_fields.sync(session, customer, find_journey=True)
    return True


async def opt_in(session: AsyncSession, customer: Customer, *, source: str) -> bool:
    if customer.nudges_opted_out_at is None:
        return False
    customer.nudges_opted_out_at = None
    await session.flush()
    log.info("nudges_opted_in", customer_id=str(customer.id), source=source)
    await _tell_zoho(session, customer, opted_out=False, source=source)
    await crm_fields.sync(session, customer, find_journey=True)
    return True


async def hold(session: AsyncSession, customer: Customer, kind: str, note: str, *,
               actor: str) -> None:
    """Hold nudges to this customer until the team lifts it."""
    if kind not in crm_fields.NUDGE_HOLDS:
        raise NudgeConfigError(f"hold is one of {list(crm_fields.NUDGE_HOLDS)}")
    customer.nudge_hold = kind
    customer.nudge_hold_note = (note or "").strip()[:300] or None
    customer.nudge_hold_at = datetime.now(timezone.utc)
    customer.nudge_hold_by = actor[:120]
    await session.flush()
    log.info("nudges_held", customer_id=str(customer.id), hold=kind, actor=actor)
    await _note(session, customer, f"WhatsApp offers: on hold ({crm_fields.NUDGE_HOLDS[kind]})",
                "No offers or follow-ups until your team lifts the hold."
                + (f"\n{customer.nudge_hold_note}" if customer.nudge_hold_note else "")
                + f"\nSet by {actor}.")
    await crm_fields.sync(session, customer, find_journey=True)


async def release(session: AsyncSession, customer: Customer, *, actor: str) -> bool:
    if not customer.nudge_hold:
        return False
    kind = customer.nudge_hold
    customer.nudge_hold = customer.nudge_hold_note = None
    customer.nudge_hold_at = customer.nudge_hold_by = None
    await session.flush()
    log.info("nudges_hold_lifted", customer_id=str(customer.id), hold=kind, actor=actor)
    await _note(session, customer, "WhatsApp offers: hold lifted",
                f"{crm_fields.NUDGE_HOLDS.get(kind, kind)} resolved; offers and follow-ups may "
                f"go to them again. Lifted by {actor}.")
    await crm_fields.sync(session, customer, find_journey=True)
    return True


async def _note(session: AsyncSession, customer: Customer, title: str, content: str) -> None:
    _, settings = await store.load_settings(session)
    record = zoho_record(customer)
    if not (settings.zoho_sync and record):
        return
    try:
        await engagement.add_note(*record, title=title,
                                  content=f"{content}\n{datetime.now(timezone.utc):%d %b %Y %H:%M} UTC")
    except (IntegrationError, ConfigurationError) as exc:
        log.warning("nudge_hold_zoho_failed", customer_id=str(customer.id), error=str(exc))


async def _tell_zoho(session: AsyncSession, customer: Customer, *, opted_out: bool,
                     source: str) -> None:
    """A note on their Lead/Contact, and the opt-out tag on a plan that has
    tags (Zoho Free has none, so the note is what the team sees there)."""
    _, settings = await store.load_settings(session)
    record = zoho_record(customer)
    if not (settings.zoho_sync and record):
        return
    how = ("replied on WhatsApp" if source == "whatsapp"
           else f"set by {source.split(':', 1)[-1]} on the admin page")
    title, content = (
        ("WhatsApp offers: opted out",
         f"No more offers or reminders ({how}). Order updates still go to them.")
        if opted_out else
        ("WhatsApp offers: opted back in", f"Offers and reminders may go to them again ({how})."))
    try:
        await engagement.add_note(*record, title=title,
                                  content=f"{content}\n{datetime.now(timezone.utc):%d %b %Y %H:%M} UTC")
        if opted_out and settings.zoho_opt_out_tag:
            await engagement.add_tags(*record, [settings.zoho_opt_out_tag])
    except IntegrationError as exc:
        if not engagement.unsupported(exc):
            log.warning("nudge_opt_out_zoho_failed", customer_id=str(customer.id), error=str(exc))
    except ConfigurationError as exc:
        log.warning("nudge_opt_out_zoho_failed", customer_id=str(customer.id), error=str(exc))
