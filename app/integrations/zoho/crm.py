"""Zoho CRM domain operations: food customers (Leads) and their orders.

Implements section 2 of the spec on Shero's CRM layout:

  * every new WhatsApp number becomes a Lead, matched on phone
  * each bot step updates its Bot Stage, with a timestamped history
  * a successful payment marks it Converted and files an Order linked to it

Contacts in that CRM are Kitchen Partners, so a Lead is never converted.
"""

from __future__ import annotations

from datetime import datetime

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models.enums import LeadStage, OrderStage
from app.integrations.zoho import fields as f
from app.integrations.zoho.client import zoho_client

log = get_logger(__name__)

# Our order stages -> the Orders module's existing Order_Status options.
ORDER_STATUS = {
    OrderStage.PENDING_PAYMENT: "Placed",
    OrderStage.PAID_SLOT_BOOKED: "Confirmed",
    OrderStage.SENT_TO_KITCHEN: "Confirmed",
    OrderStage.OUT_FOR_DELIVERY: "Dispatched",
    OrderStage.DELIVERED: "Completed",
    OrderStage.CANCELLED: "Cancelled",
    OrderStage.REFUNDED: "Cancelled",
}


def phone(whatsapp_number: str) -> str:
    """E.164 with the plus, the way Shero's records store numbers."""
    digits = "".join(ch for ch in whatsapp_number if ch.isdigit())
    return f"+{digits}"


# --- Leads -------------------------------------------------------------------
async def find_lead_by_phone(whatsapp_number: str) -> dict | None:
    """Returning-customer check (spec step 2), with or without the plus."""
    digits = phone(whatsapp_number)[1:]
    criteria = f"((Phone:equals:+{digits})or(Phone:equals:{digits}))"
    records = await zoho_client.search(f.LEADS, criteria)
    return records[0] if records else None


async def create_lead(fields: dict) -> str | None:
    lead_id = await zoho_client.create_record(f.LEADS, f.compact(fields))
    log.info("zoho_lead_created", zoho_id=lead_id)
    return lead_id


async def update_lead(lead_id: str, fields: dict) -> None:
    payload = f.compact(fields)
    if payload:
        await zoho_client.update_record(f.LEADS, lead_id, payload)


def new_lead_fields(*, whatsapp_number: str, name: str | None, email: str | None,
                    ad_id: str | None, campaign_id: str | None,
                    stage: LeadStage, history: str) -> dict:
    """A first-time number's Lead (spec step 1)."""
    first, last = f.split_name(name)
    return {
        f.L_FIRST_NAME: first or None,
        f.L_LAST_NAME: last,
        f.L_PHONE: phone(whatsapp_number),
        f.L_EMAIL: email,
        f.L_LEAD_SOURCE: f.LEAD_SOURCE_WHATSAPP,
        f.L_AD_ID: ad_id,
        f.L_CAMPAIGN_ID: campaign_id,
        f.L_BOT_STAGE: str(stage),
        f.L_BOT_STAGE_HISTORY: history,
    }


def stage_fields(stage: LeadStage, history: str) -> dict:
    return {f.L_BOT_STAGE: str(stage), f.L_BOT_STAGE_HISTORY: history}


def stage_history(stage_timestamps: dict[str, str]) -> str:
    """"2026-09-28 10:04 UTC  Cart Created", oldest first, one per line."""
    rows = sorted(stage_timestamps.items(), key=lambda item: item[1])
    lines = []
    for stage, stamp in rows:
        try:
            when = datetime.fromisoformat(stamp).strftime("%Y-%m-%d %H:%M UTC")
        except (TypeError, ValueError):
            when = str(stamp)
        lines.append(f"{when}  {stage}")
    return "\n".join(lines)[:2000]


def detail_fields(**details) -> dict:
    """Captured details in the bot's own names -> Lead fields.

    Unknown keys are ignored, so a caller cannot write an unmapped field. A
    cuisine outside the Cuisine picklist is left out rather than rejected.
    """
    first, last = f.split_name(details["name"]) if details.get("name") else ("", None)
    return {
        f.L_FIRST_NAME: first or None,
        f.L_LAST_NAME: last,
        f.L_EMAIL: details.get("email"),
        f.L_STREET: details.get("address"),
        f.L_ADDRESS_LINE_2: details.get("apartment_unit"),
        f.L_CITY: details.get("city"),
        f.L_STATE: details.get("state"),
        f.L_ZIP: details.get("postal_code"),
        f.L_LATITUDE: _coord(details.get("latitude")),
        f.L_LONGITUDE: _coord(details.get("longitude")),
        f.L_SELECTED_OUTLET: details.get("outlet_name"),
        f.L_DISTANCE_KM: details.get("distance_km"),
        f.L_CUISINE: f.cuisine_option(details.get("cuisine")),
    }


# --- Orders ------------------------------------------------------------------
def order_fields(order, *, zoho_lead_id: str | None, outlet_name: str | None,
                 cuisine: str | None) -> dict:
    """An Order record from our Order (spec section 2, "Order")."""
    address = ", ".join(p for p in (order.delivery_address, order.apartment_unit,
                                    order.postal_code) if p) or None
    return {
        f.O_ORDER_NO: order.order_number,
        f.O_LEAD: {"id": zoho_lead_id} if zoho_lead_id else None,
        f.O_CUSTOMER_NO: phone(order.contact_number) if order.contact_number else None,
        f.O_CHANNEL: f.CHANNEL_WHATSAPP_BOT,
        f.O_STATUS: ORDER_STATUS.get(OrderStage(order.stage), "Placed"),
        f.O_ADDRESS: address,
        f.O_LATITUDE: _coord(order.delivery_latitude),
        f.O_LONGITUDE: _coord(order.delivery_longitude),
        f.O_CUISINE: cuisine,
        f.O_OUTLET_NAME: outlet_name,
        f.O_DELIVERY_SLOT: order.slot_label,
        f.O_INSTRUCTIONS: order.delivery_instructions,
        f.O_ITEMS: item_rows(order.items or []),
        f.O_DELIVERY_CHARGE: order.delivery_fee,
        f.O_TAXES_AND_FEES: (order.extra_fees or 0) + (order.tax or 0),
        f.O_ORDER_TOTAL: order.total,
        f.O_STRIPE_PAYMENT_ID: order.stripe_payment_intent_id,
        f.O_PLACED_TIME: f.zoho_datetime(order.created_at),
        f.O_ACCEPTED_TIME: f.zoho_datetime(order.paid_at),
    }


def item_rows(items: list[dict]) -> list[dict]:
    """The cart as Item_Details subform rows: dish, quantity, unit price."""
    rows = []
    for item in items:
        try:
            quantity = int(item.get("quantity") or 1)
            unit_price = float(item.get("unit_price") or 0)
        except (TypeError, ValueError):
            continue
        rows.append({f.I_NAME: str(item.get("name") or item.get("retailer_id") or "Item"),
                     f.I_QUANTITY: quantity, f.I_UNIT_PRICE: unit_price})
    return rows


async def create_order(fields: dict) -> str | None:
    order_id = await zoho_client.create_record(settings.zoho_orders_module,
                                               f.compact(fields))
    log.info("zoho_order_created", order_number=fields.get(f.O_ORDER_NO),
             zoho_id=order_id)
    return order_id


async def update_order_stage(zoho_order_id: str, stage: OrderStage, *,
                             at: datetime | None = None) -> None:
    """Order_Status, plus the dispatch or delivery time when there is one."""
    payload: dict = {f.O_STATUS: ORDER_STATUS.get(stage, "Placed")}
    if stage == OrderStage.OUT_FOR_DELIVERY:
        payload[f.O_DISPATCHED_TIME] = f.zoho_datetime(at)
    if stage == OrderStage.DELIVERED:
        payload[f.O_DELIVERED_TIME] = f.zoho_datetime(at)
    await zoho_client.update_record(settings.zoho_orders_module, zoho_order_id,
                                    f.compact(payload))
    log.info("zoho_order_stage_updated", zoho_id=zoho_order_id, stage=str(stage))


def _coord(value) -> str | None:
    return None if value is None else f"{float(value):.6f}"
