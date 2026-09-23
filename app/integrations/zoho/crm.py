"""Zoho CRM domain operations: leads, contacts and orders.

Implements section 2 of the spec:
  * every new WhatsApp number becomes a Lead (matched on number, never duplicated)
  * each bot step updates the Lead stage, with a timestamp per change
  * a successful payment converts the Lead to a Contact and files an Order
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models.enums import LeadStage, OrderStage
from app.integrations.zoho import fields as f
from app.integrations.zoho.client import zoho_client

log = get_logger(__name__)


# --- Leads -------------------------------------------------------------------
async def find_lead_by_phone(whatsapp_number: str) -> dict | None:
    """Returning-customer check (spec step 2)."""
    records = await zoho_client.search("Leads", f"({f.PHONE}:equals:{whatsapp_number})")
    return records[0] if records else None


async def find_contact_by_phone(whatsapp_number: str) -> dict | None:
    """A converted customer lives in Contacts, not Leads."""
    records = await zoho_client.search("Contacts", f"({f.PHONE}:equals:{whatsapp_number})")
    return records[0] if records else None


async def create_lead(
    *,
    whatsapp_number: str,
    name: str | None = None,
    email: str | None = None,
    ad_id: str | None = None,
    campaign_id: str | None = None,
    stage: LeadStage = LeadStage.NEW_ENQUIRY,
) -> str | None:
    """Create the Lead for a first-time number (spec step 1).

    Uses upsert keyed on Phone so a webhook replay or a race between two
    inbound messages cannot produce a duplicate Lead.
    """
    first, last = f.split_name(name)
    payload = f.compact({
        f.LAST_NAME: last,
        f.FIRST_NAME: first or None,
        f.COMPANY: f.DEFAULT_COMPANY,
        f.PHONE: whatsapp_number,
        f.MOBILE: whatsapp_number,
        f.EMAIL: email,
        f.LEAD_SOURCE: "Meta Ad" if ad_id or campaign_id else "WhatsApp",
        f.LEAD_STATUS: str(stage),
        f.CUSTOM_WHATSAPP_NUMBER: whatsapp_number,
        f.CUSTOM_AD_ID: ad_id,
        f.CUSTOM_CAMPAIGN_ID: campaign_id,
        f.CUSTOM_STAGE_TIMESTAMPS: json.dumps({str(stage): _now_iso()}),
    })
    lead_id = await zoho_client.upsert_record("Leads", payload, [f.PHONE])
    log.info("zoho_lead_created", lead_id=lead_id, stage=str(stage))
    return lead_id


async def update_lead_stage(lead_id: str, stage: LeadStage,
                            stage_timestamps: dict[str, str]) -> None:
    """Move the Lead to a new stage and persist the full timestamp history.

    The history is written as a JSON blob in one multi-line text field, which
    keeps reporting on drop-off possible without 11 separate date fields.
    """
    await zoho_client.update_record("Leads", lead_id, f.compact({
        f.LEAD_STATUS: str(stage),
        f.CUSTOM_STAGE_TIMESTAMPS: json.dumps(stage_timestamps),
    }))
    log.info("zoho_lead_stage_updated", lead_id=lead_id, stage=str(stage))


async def update_lead_details(lead_id: str, **details) -> None:
    """Patch captured details onto the Lead as the conversation progresses.

    Accepts the domain names used by the bot; unknown keys are ignored so a
    caller cannot accidentally write an unmapped field.
    """
    mapping = {
        "name": None,  # handled below (splits into first/last)
        "email": f.EMAIL,
        "address": f.STREET,
        "city": f.CITY,
        "state": f.STATE,
        "postal_code": f.ZIP_CODE,
        "apartment_unit": f.CUSTOM_APARTMENT_UNIT,
        "outlet_name": f.CUSTOM_SELECTED_OUTLET,
        "distance_km": f.CUSTOM_DISTANCE_KM,
        "cuisine": f.CUSTOM_CUISINE_PREFERENCE,
    }
    payload: dict = {}
    for key, value in details.items():
        if value is None:
            continue
        if key == "name":
            first, last = f.split_name(value)
            payload[f.LAST_NAME] = last
            if first:
                payload[f.FIRST_NAME] = first
        elif key in mapping and mapping[key]:
            payload[mapping[key]] = value

    if not payload:
        return
    await zoho_client.update_record("Leads", lead_id, f.compact(payload))


async def convert_lead_to_contact(lead_id: str) -> str | None:
    """Payment succeeded -> Lead becomes a Contact (spec step 16)."""
    result = await zoho_client.convert_lead(lead_id)
    contact_id = result.get("Contacts")
    log.info("zoho_lead_converted", lead_id=lead_id, contact_id=contact_id)
    return contact_id


# --- Orders ------------------------------------------------------------------
async def create_order(
    *,
    order_number: str,
    contact_id: str | None,
    outlet_name: str | None,
    items: list[dict],
    dish_total,
    delivery_charge,
    tax,
    total,
    slot_label: str | None,
    stripe_payment_id: str | None,
    stage: OrderStage,
    delivery_address: str | None = None,
) -> str | None:
    """File the Order record under the converted Contact."""
    payload = f.compact({
        f.ORDER_NUMBER: order_number,
        f.ORDER_CONTACT: {"id": contact_id} if contact_id else None,
        f.ORDER_OUTLET: outlet_name,
        f.ORDER_ITEMS: format_items(items),
        f.ORDER_DISH_TOTAL: dish_total,
        f.ORDER_DELIVERY_CHARGE: delivery_charge,
        f.ORDER_TAX: tax,
        f.ORDER_TOTAL: total,
        f.ORDER_SLOT: slot_label,
        f.ORDER_STRIPE_PAYMENT_ID: stripe_payment_id,
        f.ORDER_STAGE: str(stage),
        f.ORDER_DELIVERY_ADDRESS: delivery_address,
    })
    order_id = await zoho_client.create_record(settings.zoho_orders_module, payload)
    log.info("zoho_order_created", order_number=order_number, zoho_id=order_id)
    return order_id


async def update_order_stage(zoho_order_id: str, stage: OrderStage, *,
                             delivered_at: datetime | None = None) -> None:
    payload = {f.ORDER_STAGE: str(stage)}
    if delivered_at is not None:
        payload[f.ORDER_DELIVERED_TIME] = delivered_at.astimezone(timezone.utc).isoformat()
    await zoho_client.update_record(settings.zoho_orders_module, zoho_order_id,
                                    f.compact(payload))
    log.info("zoho_order_stage_updated", zoho_id=zoho_order_id, stage=str(stage))


def format_items(items: list[dict]) -> str:
    """Render the cart as one text block: '2 x Chicken Biryani - $25.00'."""
    lines = []
    for item in items or []:
        qty = item.get("quantity", 1)
        name = item.get("name") or item.get("retailer_id", "item")
        line_total = item.get("line_total")
        suffix = f" - {line_total}" if line_total is not None else ""
        lines.append(f"{qty} x {name}{suffix}")
    return "\n".join(lines)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
