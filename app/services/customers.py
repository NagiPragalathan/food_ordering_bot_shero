"""Customer and conversation lookup.

Every inbound WhatsApp message lands here first: find (or create) the customer
for the number, and load the conversation row that says where they are in the
flow.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Conversation, ConversationStep, Customer, LeadStage
from app.integrations.gallabox.client import normalise_phone

log = get_logger(__name__)


async def get_customer(session: AsyncSession, whatsapp_number: str) -> Customer | None:
    number = normalise_phone(whatsapp_number)
    result = await session.execute(
        select(Customer).where(Customer.whatsapp_number == number)
    )
    return result.scalar_one_or_none()


async def get_or_create_customer(
    session: AsyncSession,
    whatsapp_number: str,
    *,
    ad_id: str | None = None,
    campaign_id: str | None = None,
    referral_payload: dict | None = None,
) -> tuple[Customer, bool]:
    """Return (customer, was_created).

    Ad attribution is only written on creation, so a returning customer keeps
    the campaign that originally brought them in.
    """
    number = normalise_phone(whatsapp_number)
    customer = await get_customer(session, number)
    if customer:
        customer.last_seen_at = datetime.now(timezone.utc)
        return customer, False

    customer = Customer(
        whatsapp_number=number,
        ad_id=ad_id,
        campaign_id=campaign_id,
        referral_payload=referral_payload or {},
        lead_source="Meta Ad" if (ad_id or campaign_id) else "WhatsApp",
        lead_stage=LeadStage.NEW_ENQUIRY,
        stage_timestamps={str(LeadStage.NEW_ENQUIRY): _now_iso()},
        last_seen_at=datetime.now(timezone.utc),
    )
    session.add(customer)
    await session.flush()
    log.info("customer_created", customer_id=str(customer.id), ad_id=ad_id)
    return customer, True


async def get_or_create_conversation(session: AsyncSession,
                                     customer: Customer) -> Conversation:
    result = await session.execute(
        select(Conversation).where(Conversation.customer_id == customer.id)
    )
    conversation = result.scalar_one_or_none()
    if conversation:
        return conversation

    conversation = Conversation(customer_id=customer.id, step=ConversationStep.START)
    session.add(conversation)
    await session.flush()
    return conversation


def advance(conversation: Conversation, step: ConversationStep) -> None:
    """Move the conversation to `step`, remembering where it came from.

    `previous_step` is what makes "Edit -> back to the menu" (spec step 14)
    and the re-ask paths possible without a separate history table.
    """
    if conversation.step != str(step):
        conversation.previous_step = conversation.step
    conversation.step = str(step)
    conversation.last_message_at = datetime.now(timezone.utc)


def record_stage(customer: Customer, stage: LeadStage) -> bool:
    """Set the lead stage and stamp the time.

    Returns True when the stage actually changed, so the caller only pushes to
    Zoho on a real transition. Re-entering a stage (a customer editing their
    cart, say) keeps the original timestamp, which is what makes the drop-off
    report meaningful.
    """
    if customer.lead_stage == str(stage):
        return False

    customer.lead_stage = str(stage)
    customer.stage_timestamps = {
        **(customer.stage_timestamps or {}),
        str(stage): _now_iso(),
    }
    log.info("lead_stage_changed", customer_id=str(customer.id), stage=str(stage))
    return True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
