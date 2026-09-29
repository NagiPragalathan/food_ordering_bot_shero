"""Messages we start, to numbers that have not written to us.

Everything else in the bot answers somebody. This module is the one place that
opens a conversation, which makes it the one place that has to cope with
knowing nothing about the person yet: no name, no email, often no record at
all.

WhatsApp only allows an approved template to open a conversation, so this
sends `shero_welcome` and lets its **Order Now** button carry the customer
into the ordinary flow. Onboarding then asks for the name, because the
conversation is still sitting at its first step.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import IntegrationError
from app.core.logging import get_logger
from app.db.models import ConversationStep, Customer
from app.integrations.gallabox import templates as tpl
from app.integrations.gallabox.sender import current_sender
from app.services import allowlist
from app.services import customers as customer_service
from app.services.crm_sync import ensure_record

log = get_logger(__name__)


class AlreadyWithAgent(Exception):
    """The conversation belongs to a human right now."""


class NotAllowlisted(Exception):
    """Test mode is on and this number is not on the list."""


async def send_welcome(session: AsyncSession, whatsapp_number: str) -> tuple[Customer, bool]:
    """Open a conversation with `whatsapp_number`.

    Creates the lead if there is none, so the reply has somewhere to land and
    the customer appears in the funnel from the moment we contacted them.

    Returns `(customer, lead_was_created)`.

    Raises `AlreadyWithAgent` if one of your team is mid-conversation with
    this person - an automated greeting dropped into that would be rude and
    confusing.
    """
    # Test mode must never reach a stranger - checked before a lead exists.
    if not allowlist.permits(whatsapp_number):
        raise NotAllowlisted(
            f"{whatsapp_number} is not in BOT_ALLOWED_NUMBERS; not sending.")

    customer, created = await customer_service.get_or_create_customer(
        session, whatsapp_number)
    conversation = await customer_service.get_or_create_conversation(session, customer)

    if conversation.step == ConversationStep.HANDED_OVER:
        raise AlreadyWithAgent(
            f"{customer.whatsapp_number} is with an agent; not sending a greeting.")

    # Zoho should know about the lead before we message them, but a CRM that is
    # unreachable must not stop us saying hello.
    try:
        await ensure_record(customer)
    except IntegrationError as exc:
        log.warning("outreach_lead_sync_failed",
                    customer_id=str(customer.id), error=str(exc))

    await current_sender().send_template(
        customer.whatsapp_number,
        tpl.WELCOME,
        # "there" when we have never been told a name. Onboarding asks for it
        # as soon as they tap Order Now.
        customer.greeting_name,
    )

    log.info("welcome_sent", customer_id=str(customer.id),
             lead_created=created, named=bool(customer.name))
    return customer, created
