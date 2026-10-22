"""STOP and START OFFERS: a customer's own say over nudges.

Honoured whatever the master switch says - someone who asks to stop is
stopped, even if nudges are off today and turned on next month. Only nudges
stop: order updates, payment links and receipts still reach them.

The keywords and replies are in the nudge settings. A message has to be the
keyword and nothing else, so "stop the order" is not an opt-out.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConfigurationError, IntegrationError
from app.core.logging import get_logger
from app.db.models import Customer
from app.integrations.zoho import engagement
from app.services.nudges import store
from app.services.nudges.runner import zoho_record

log = get_logger(__name__)


def _normal(text: str) -> str:
    return " ".join((text or "").lower().replace("-", " ").split())


async def handle_keyword(ctx) -> bool:
    """Act on STOP / START OFFERS from the conversation engine. True if it was one."""
    text = _normal(ctx.choice)
    if not text or len(text) > 40:
        return False
    _, settings = await store.load_settings(ctx.session)
    if text in settings.stop_keywords:
        await opt_out(ctx.session, ctx.customer, source="whatsapp")
        await ctx.reply_text(settings.opt_out_reply)
        return True
    if text in settings.start_keywords:
        await opt_in(ctx.session, ctx.customer, source="whatsapp")
        await ctx.reply_text(settings.opt_in_reply)
        return True
    return False


async def opt_out(session: AsyncSession, customer: Customer, *, source: str) -> bool:
    """Stop nudges to this customer. True if they were not already opted out."""
    if customer.nudges_opted_out_at is not None:
        return False
    customer.nudges_opted_out_at = datetime.now(timezone.utc)
    await session.flush()
    log.info("nudges_opted_out", customer_id=str(customer.id), source=source)
    await _tag_in_zoho(session, customer)
    return True


async def opt_in(session: AsyncSession, customer: Customer, *, source: str) -> bool:
    if customer.nudges_opted_out_at is None:
        return False
    customer.nudges_opted_out_at = None
    await session.flush()
    log.info("nudges_opted_in", customer_id=str(customer.id), source=source)
    return True


async def _tag_in_zoho(session: AsyncSession, customer: Customer) -> None:
    _, settings = await store.load_settings(session)
    record = zoho_record(customer)
    if not (settings.zoho_sync and settings.zoho_opt_out_tag and record):
        return
    try:
        await engagement.add_tags(*record, [settings.zoho_opt_out_tag])
    except (IntegrationError, ConfigurationError) as exc:
        log.warning("nudge_opt_out_zoho_failed", customer_id=str(customer.id), error=str(exc))
