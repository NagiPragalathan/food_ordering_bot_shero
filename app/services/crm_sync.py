"""Zoho CRM synchronisation (spec section 2).

Design rule for this whole module: **CRM sync never breaks the conversation.**
Zoho is the reporting system, not the system of record for an order. If it is
down or misconfigured, the customer must still be able to browse, pay and eat.
So every function here swallows integration failures, logs them loudly, and
returns a success flag the caller may ignore.

The local database keeps the authoritative stage and timestamps, which means a
failed push can be replayed later without data loss.
"""

from __future__ import annotations

from datetime import datetime

from app.core.exceptions import ConfigurationError, IntegrationError
from app.core.logging import get_logger
from app.db.models import Customer, LeadStage, Order, OrderStage
from app.integrations.zoho import crm
from app.services.customers import record_stage

log = get_logger(__name__)


async def ensure_lead(customer: Customer) -> str | None:
    """Make sure the customer has a Zoho Lead, creating one if needed.

    A customer who already converted has a Contact instead; no Lead is made
    for them, matching the spec rule that a returning customer gets a new
    Order under the existing Contact rather than a duplicate Lead.
    """
    if customer.zoho_lead_id or customer.zoho_contact_id:
        return customer.zoho_lead_id

    try:
        # Someone may already exist in Zoho from a previous deployment or a
        # manual import, so look before creating.
        existing_contact = await crm.find_contact_by_phone(customer.whatsapp_number)
        if existing_contact:
            customer.zoho_contact_id = existing_contact.get("id")
            customer.is_converted = True
            log.info("zoho_existing_contact_linked",
                     contact_id=customer.zoho_contact_id)
            return None

        existing_lead = await crm.find_lead_by_phone(customer.whatsapp_number)
        if existing_lead:
            customer.zoho_lead_id = existing_lead.get("id")
            log.info("zoho_existing_lead_linked", lead_id=customer.zoho_lead_id)
            return customer.zoho_lead_id

        customer.zoho_lead_id = await crm.create_lead(
            whatsapp_number=customer.whatsapp_number,
            name=customer.name,
            email=customer.email,
            ad_id=customer.ad_id,
            campaign_id=customer.campaign_id,
            stage=LeadStage(customer.lead_stage),
        )
        return customer.zoho_lead_id

    except (IntegrationError, ConfigurationError, ValueError) as exc:
        log.error("zoho_ensure_lead_failed", customer_id=str(customer.id),
                  error=str(exc))
        return None


async def advance_stage(customer: Customer, stage: LeadStage) -> bool:
    """Record a stage locally, then mirror it to Zoho.

    Returns True when the stage changed locally - the part that always
    succeeds and drives our own reporting.
    """
    changed = record_stage(customer, stage)
    if not changed:
        return False

    lead_id = await ensure_lead(customer)
    if not lead_id:
        return True  # local record updated; nothing to push (or push failed)

    try:
        await crm.update_lead_stage(lead_id, stage, customer.stage_timestamps or {})
    except (IntegrationError, ConfigurationError) as exc:
        log.error("zoho_stage_push_failed", lead_id=lead_id, stage=str(stage),
                  error=str(exc))
    return True


async def push_details(customer: Customer, **details) -> None:
    """Mirror captured details (name, email, address, outlet) onto the Lead."""
    lead_id = await ensure_lead(customer)
    if not lead_id:
        return
    try:
        await crm.update_lead_details(lead_id, **details)
    except (IntegrationError, ConfigurationError) as exc:
        log.error("zoho_details_push_failed", lead_id=lead_id, error=str(exc))


async def convert_and_record_order(customer: Customer, order: Order,
                                   outlet_name: str | None) -> None:
    """Payment succeeded: convert the Lead and file the Order (spec step 16)."""
    # 1. Lead -> Contact, unless already converted from an earlier order.
    if not customer.zoho_contact_id:
        lead_id = await ensure_lead(customer)
        if lead_id:
            try:
                customer.zoho_contact_id = await crm.convert_lead_to_contact(lead_id)
                customer.is_converted = True
            except (IntegrationError, ConfigurationError) as exc:
                log.error("zoho_convert_failed", lead_id=lead_id, error=str(exc))

    record_stage(customer, LeadStage.CONVERTED)

    # 2. File the Order under the Contact.
    if order.zoho_order_id:
        return  # already filed; a webhook replay must not duplicate it

    try:
        order.zoho_order_id = await crm.create_order(
            order_number=order.order_number,
            contact_id=customer.zoho_contact_id,
            outlet_name=outlet_name,
            items=order.items or [],
            dish_total=order.dish_total,
            delivery_charge=order.delivery_fee + order.extra_fees,
            tax=order.tax,
            total=order.total,
            slot_label=order.slot_label,
            stripe_payment_id=order.stripe_payment_intent_id,
            stage=OrderStage(order.stage),
            delivery_address=_full_address(order),
        )
    except (IntegrationError, ConfigurationError) as exc:
        log.error("zoho_order_create_failed", order_number=order.order_number,
                  error=str(exc))


async def push_order_stage(order: Order, stage: OrderStage, *,
                           delivered_at: datetime | None = None) -> None:
    """Mirror an order stage change into Zoho (spec steps 17-18)."""
    if not order.zoho_order_id:
        log.info("zoho_order_stage_skipped", order_number=order.order_number,
                 reason="no zoho order id")
        return
    try:
        await crm.update_order_stage(order.zoho_order_id, stage,
                                     delivered_at=delivered_at)
    except (IntegrationError, ConfigurationError) as exc:
        log.error("zoho_order_stage_failed", order_number=order.order_number,
                  error=str(exc))


def _full_address(order: Order) -> str:
    parts = [order.delivery_address, order.apartment_unit, order.postal_code]
    return ", ".join(p for p in parts if p)
