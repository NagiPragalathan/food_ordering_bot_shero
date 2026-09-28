"""Zoho CRM synchronisation (spec section 2).

Design rule for this whole module: **CRM sync never breaks the conversation.**
Zoho is the reporting system, not the system of record for an order. If it is
down or misconfigured, the customer must still be able to browse, pay and eat.
So every function here catches integration failures, logs them loudly, and
returns a success flag the caller may ignore.

The local database keeps the authoritative stage and timestamps, which means a
failed push can be replayed later without data loss.

In Shero's CRM food customers are Leads and paid orders go in Orders (see
integrations/zoho/crm.py). The spec's stages are the Lead's Bot Stage;
"converted" is Bot Stage = Converted, because a Contact there is a Kitchen
Partner and a Lead is never converted.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.exceptions import ConfigurationError, IntegrationError
from app.core.logging import get_logger
from app.db.models import Customer, LeadStage, Order, OrderStage
from app.integrations.zoho import crm
from app.services.customers import record_stage

log = get_logger(__name__)

ZOHO_ERRORS = (IntegrationError, ConfigurationError)


async def ensure_lead(customer: Customer) -> str | None:
    """Make sure the customer has a Zoho Lead; return its id.

    Someone may already be in Zoho from an earlier order or a manual entry,
    so it looks the number up before creating anything - one Lead per
    WhatsApp number (spec section 2).
    """
    if customer.zoho_lead_id:
        return customer.zoho_lead_id

    try:
        existing = await crm.find_lead_by_phone(customer.whatsapp_number)
        if existing:
            customer.zoho_lead_id = existing.get("id")
            log.info("zoho_existing_lead_linked", zoho_id=customer.zoho_lead_id)
            return customer.zoho_lead_id

        customer.zoho_lead_id = await crm.create_lead(crm.new_lead_fields(
            whatsapp_number=customer.whatsapp_number,
            name=customer.name,
            email=customer.email,
            ad_id=customer.ad_id,
            campaign_id=customer.campaign_id,
            stage=LeadStage(customer.lead_stage),
            history=crm.stage_history(customer.stage_timestamps or {}),
        ))
        return customer.zoho_lead_id

    except (*ZOHO_ERRORS, ValueError) as exc:
        log.error("zoho_ensure_lead_failed", customer_id=str(customer.id),
                  error=str(exc))
        return None


# The order a customer walks through the funnel. A stage outside it (Not
# Serviceable, Payment Abandoned / Failed, Converted) ends one attempt, and
# the next attempt may start again from anywhere.
FUNNEL: tuple[LeadStage, ...] = (
    LeadStage.NEW_ENQUIRY,
    LeadStage.DETAILS_CAPTURED,
    LeadStage.CUISINE_SELECTED,
    LeadStage.CART_CREATED,
    LeadStage.OUTLET_SELECTED,
    LeadStage.SLOT_SELECTED,
    LeadStage.PAYMENT_LINK_SENT,
)


def is_backwards(current: str | None, stage: LeadStage) -> bool:
    """True when `stage` is earlier in the funnel than where the customer is.

    The web page reports a stage on every cart edit and address check; a
    customer who adds one more dish after picking a slot has not gone back
    to "Cart Created", and the drop-off report must not say they did.
    """
    try:
        return FUNNEL.index(LeadStage(current)) > FUNNEL.index(stage)
    except ValueError:      # current or stage outside the funnel
        return False


async def advance_stage(customer: Customer, stage: LeadStage, *,
                        forward_only: bool = False) -> bool:
    """Record a stage locally, then mirror it to Zoho's Bot Stage.

    Returns True when the stage changed locally - the part that always
    succeeds and drives our own reporting. `forward_only` ignores a stage
    that would move the customer backwards (see is_backwards).
    """
    if forward_only and is_backwards(customer.lead_stage, stage):
        return False
    changed = record_stage(customer, stage)
    if not changed:
        return False

    zoho_id = await ensure_lead(customer)
    if zoho_id:
        # Also right after creating: a record found by phone may be at any stage.
        await _update(zoho_id, crm.stage_fields(
            stage, crm.stage_history(customer.stage_timestamps or {})))
    return True


async def push_details(customer: Customer, **details) -> None:
    """Mirror captured details (name, email, address, outlet, ...) to Zoho."""
    zoho_id = await ensure_lead(customer)
    if zoho_id:
        await _update(zoho_id, crm.detail_fields(**details))


async def convert_and_record_order(customer: Customer, order: Order,
                                   outlet_name: str | None) -> None:
    """Payment succeeded: mark the Lead Converted and file the Order.

    The spec's "Lead -> Contact" step. In Shero's CRM a Contact is a Kitchen
    Partner, so the Lead stays a Lead with Bot Stage Converted.
    """
    record_stage(customer, LeadStage.CONVERTED)
    customer.is_converted = True
    zoho_id = await ensure_lead(customer)
    if zoho_id:
        await _update(zoho_id, crm.stage_fields(
            LeadStage.CONVERTED, crm.stage_history(customer.stage_timestamps or {})))

    if order.zoho_order_id:
        return  # already filed; a webhook replay must not duplicate it

    try:
        order.zoho_order_id = await crm.create_order(crm.order_fields(
            order, zoho_lead_id=zoho_id, outlet_name=outlet_name,
            cuisine=customer.cuisine_preference,
        ))
    except ZOHO_ERRORS as exc:
        log.error("zoho_order_create_failed", order_number=order.order_number,
                  error=str(exc))


async def push_order_stage(order: Order, stage: OrderStage, *,
                           delivered_at: datetime | None = None) -> None:
    """Mirror an order stage change into Zoho's Order_Status (spec 17-18)."""
    if not order.zoho_order_id:
        log.info("zoho_order_stage_skipped", order_number=order.order_number,
                 reason="no zoho order id")
        return
    at = delivered_at or datetime.now(timezone.utc)
    try:
        await crm.update_order_stage(order.zoho_order_id, stage, at=at)
    except ZOHO_ERRORS as exc:
        log.error("zoho_order_stage_failed", order_number=order.order_number,
                  error=str(exc))


# --- helpers -----------------------------------------------------------------
async def _update(zoho_id: str, fields: dict) -> None:
    try:
        await crm.update_lead(zoho_id, fields)
    except ZOHO_ERRORS as exc:
        log.error("zoho_lead_update_failed", zoho_id=zoho_id, error=str(exc))
