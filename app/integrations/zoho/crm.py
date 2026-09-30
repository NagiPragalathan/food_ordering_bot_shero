"""Zoho CRM domain operations (spec section 2).

  * every new WhatsApp number becomes a Lead, matched on phone
  * each bot step updates its Bot Stage, with a timestamped history
  * the first successful payment converts the Lead into a Contact, and every
    paid order is filed in the Orders module under that Contact
  * each dish on that order is an Order Item linked to the Zoho Product the
    dish is, and the order links to the Vendor the kitchen is

Field names live in fields.py. The sync policy - what to do when Zoho is
down, which stage counts as backwards - is services/crm_sync.py, and the
menu-to-Products / kitchens-to-Vendors side is services/catalogue_sync.py.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.core.config import settings
from app.core.exceptions import IntegrationError
from app.core.logging import get_logger
from app.db.models.enums import LeadStage, OrderStage
from app.integrations.zoho import fields as f
from app.integrations.zoho.client import zoho_client

log = get_logger(__name__)


def phone(whatsapp_number: str) -> str:
    """E.164 with the plus, the way Zoho stores numbers."""
    digits = "".join(ch for ch in whatsapp_number if ch.isdigit())
    return f"+{digits}"


# --- Leads and Contacts --------------------------------------------------------
async def find_by_phone(module: str, whatsapp_number: str) -> dict | None:
    """Returning-customer check (spec step 2).

    Matches Phone or Mobile, with or without the plus, so a record typed in
    by hand is found too.
    """
    digits = phone(whatsapp_number)[1:]
    by_phone = f"(({f.PHONE}:equals:+{digits})or({f.PHONE}:equals:{digits}))"
    by_mobile = f"(({f.MOBILE}:equals:+{digits})or({f.MOBILE}:equals:{digits}))"
    records = await zoho_client.search(module, f"({by_phone}or{by_mobile})")
    return records[0] if records else None


async def get_record(module: str, record_id: str) -> dict | None:
    """The record, or None when it no longer exists (deleted in Zoho)."""
    return await zoho_client.get_record(module, record_id)


async def create_lead(fields: dict) -> str | None:
    try:
        lead_id = await zoho_client.create_record(f.LEADS, f.compact(fields))
    except IntegrationError as exc:
        if not _missing_mandatory(exc, f.COMPANY):
            raise
        # Zoho's API does not require Company whatever the layout says, but
        # an org can be set up to. The person's name then keeps the bot
        # working (an Account of that name appears when they convert).
        log.warning("zoho_lead_company_required_using_name")
        name = " ".join(p for p in (fields.get(f.FIRST_NAME), fields.get(f.LAST_NAME)) if p)
        lead_id = await zoho_client.create_record(
            f.LEADS, f.compact({**fields, f.COMPANY: name or f.NAME_PLACEHOLDER}))
    log.info("zoho_lead_created", zoho_id=lead_id)
    return lead_id


async def update_record(module: str, record_id: str, fields: dict) -> None:
    payload = f.compact(fields)
    if payload:
        await zoho_client.update_record(module, record_id, payload)


async def convert_lead(lead_id: str) -> str:
    """Zoho's Lead -> Contact conversion (spec step 16); returns the Contact id."""
    contact_id = await zoho_client.convert_lead(lead_id)
    log.info("zoho_lead_converted", lead_id=lead_id, contact_id=contact_id)
    return contact_id


def is_converted_error(exc: IntegrationError) -> bool:
    """True when Zoho refused a write because the Lead has been converted."""
    record = exc.payload if isinstance(exc.payload, dict) else {}
    return (record.get("code") == "INVALID_DATA"
            and "converted" in str(record.get("message", "")).lower())


async def relink_order(zoho_order_id: str, contact_id: str) -> None:
    """Move an order filed under a Lead to the Contact that Lead became."""
    # Not compacted: the explicit null is what clears the Lead lookup.
    await zoho_client.update_record(settings.zoho_orders_module, zoho_order_id,
                                    {f.O_CONTACT: {"id": contact_id}, f.O_LEAD: None})
    log.info("zoho_order_relinked", zoho_id=zoho_order_id, contact_id=contact_id)


def new_lead_fields(*, whatsapp_number: str, name: str | None, email: str | None,
                    lead_source: str | None, ad_id: str | None,
                    campaign_id: str | None, stage: LeadStage, history: str,
                    whatsapp_profile_name: str | None = None) -> dict:
    """A first-time number's Lead (spec step 1)."""
    first, last = f.split_name(name)
    return {
        f.FIRST_NAME: first or None,
        f.LAST_NAME: last,
        f.PHONE: phone(whatsapp_number),
        f.EMAIL: email,
        f.LEAD_SOURCE: f.lead_source_option(lead_source),
        f.AD_ID: ad_id,
        f.CAMPAIGN_ID: campaign_id,
        f.LEAD_STATUS: str(stage),
        f.BOT_STAGE: str(stage),
        f.BOT_STAGE_HISTORY: history,
        f.WHATSAPP_PROFILE_NAME: whatsapp_profile_name,
    }


def stage_fields(module: str, stage: LeadStage, history: str) -> dict:
    """Bot Stage and its history; on a Lead also Lead Status, Zoho's own
    stage field, so the stage bar and the standard reports show the funnel."""
    fields = {f.BOT_STAGE: str(stage), f.BOT_STAGE_HISTORY: history}
    if module == f.LEADS:
        fields[f.LEAD_STATUS] = str(stage)
    return fields


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


def detail_fields(module: str, **details) -> dict:
    """Captured details in the bot's own names -> the module's fields.

    Unknown keys are ignored, so a caller cannot write an unmapped field.
    """
    address = f.ADDRESS_FIELDS[module]
    has_name = bool(details.get("name"))
    first, last = f.split_name(details["name"]) if has_name else ("", None)
    return {
        # With a name, First Name is always sent - blank for a one-word name -
        # so an old first name ("Nagipragalathan") does not stay next to the
        # new last name ("Nagi"). Without one, it is left alone.
        f.FIRST_NAME: first if has_name else None,
        f.LAST_NAME: last,
        f.EMAIL: details.get("email"),
        address["street"]: details.get("address"),
        f.ADDRESS_LINE_2: details.get("apartment_unit"),
        address["city"]: details.get("city"),
        address["state"]: details.get("state"),
        address["zip"]: details.get("postal_code"),
        f.LATITUDE: _coord(details.get("latitude")),
        f.LONGITUDE: _coord(details.get("longitude")),
        f.SELECTED_OUTLET: details.get("outlet_name"),
        f.DISTANCE_KM: details.get("distance_km"),
        f.CUISINE: f.cuisine_label(details.get("cuisine")),
        f.WHATSAPP_PROFILE_NAME: details.get("whatsapp_profile_name"),
    }


async def existing_ids(module: str, ids: list[str]) -> set[str]:
    """Which of these record ids Zoho still has (deleted ones drop out)."""
    return await zoho_client.existing_ids(module, ids)


async def org_id() -> str | None:
    """Which Zoho org the connection belongs to."""
    return await zoho_client.org_id()


# --- Orders ------------------------------------------------------------------
def order_fields(order, *, zoho_contact_id: str | None, zoho_lead_id: str | None,
                 outlet_name: str | None, cuisine: str | None,
                 zoho_vendor_id: str | None = None,
                 delivery_at: datetime | None = None) -> dict:
    """An Order record from our Order (spec section 2, "Order").

    Linked to the Contact; to the Lead only when the customer could not be
    converted, so no order is ever filed without its customer. Linked to the
    kitchen's Vendor when that has been synced.
    """
    address = ", ".join(p for p in (order.delivery_address, order.apartment_unit,
                                    order.postal_code) if p) or None
    return {
        settings.zoho_orders_name_field: order.order_number,
        f.O_CONTACT: {"id": zoho_contact_id} if zoho_contact_id else None,
        f.O_LEAD: {"id": zoho_lead_id} if zoho_lead_id and not zoho_contact_id else None,
        f.O_CUSTOMER_NO: phone(order.contact_number) if order.contact_number else None,
        f.O_CHANNEL: f.CHANNEL_WHATSAPP_BOT,
        f.O_STATUS: str(OrderStage(order.stage)),
        f.O_ADDRESS: address,
        f.O_LATITUDE: _coord(order.delivery_latitude),
        f.O_LONGITUDE: _coord(order.delivery_longitude),
        f.O_CUISINE: f.cuisine_label(cuisine),
        **order_link_fields(zoho_vendor_id=zoho_vendor_id, outlet_name=outlet_name,
                            delivery_at=delivery_at),
        f.O_DELIVERY_SLOT: order.slot_label,
        f.O_ITEMS: item_lines(order.items or []),
        f.O_INSTRUCTIONS: order.delivery_instructions,
        f.O_DISH_TOTAL: order.dish_total,
        f.O_DELIVERY_CHARGE: order.delivery_fee,
        f.O_TAXES_AND_FEES: (order.extra_fees or 0) + (order.tax or 0),
        f.O_ORDER_TOTAL: order.total,
        f.O_STRIPE_PAYMENT_ID: order.stripe_payment_intent_id,
        f.O_PLACED_TIME: f.zoho_datetime(order.created_at),
        f.O_PAID_TIME: f.zoho_datetime(order.paid_at),
    }


def order_link_fields(*, zoho_vendor_id: str | None, outlet_name: str | None,
                      delivery_at: datetime | None) -> dict:
    """The kitchen (as its Vendor and as text) and the delivery time.

    Separate from order_fields so an order filed earlier can be given these
    later (the admin's Push to Zoho), without rewriting the rest.
    """
    return {
        f.O_OUTLET: {"id": zoho_vendor_id} if zoho_vendor_id else None,
        f.O_OUTLET_NAME: outlet_name,
        f.O_DELIVERY_TIME: f.zoho_datetime(delivery_at),
    }


def item_lines(items: list[dict]) -> str | None:
    """The cart as text, one dish per line: `2 x Sambar @ 9.50 = 19.00`."""
    lines = []
    for item in items:
        parsed = f.parse_line(item)
        if parsed is None:
            continue
        _, name, quantity, unit_price = parsed
        lines.append(f"{quantity} x {name} @ {unit_price:.2f} = {quantity * unit_price:.2f}")
    return "\n".join(lines)[:2000] or None


async def create_order(fields: dict) -> str | None:
    order_id = await zoho_client.create_record(settings.zoho_orders_module,
                                               f.compact(fields))
    log.info("zoho_order_created", order_number=fields.get(settings.zoho_orders_name_field),
             zoho_id=order_id)
    return order_id


async def update_order(zoho_order_id: str, fields: dict) -> None:
    """Set more on an Order already filed; None values are left as they are."""
    payload = f.compact(fields)
    if payload:
        await zoho_client.update_record(settings.zoho_orders_module, zoho_order_id, payload)


# --- Sales Orders: the order with a native product grid ------------------------
SALES_ORDER_STATUS = {
    OrderStage.PAID_SLOT_BOOKED: f.SO_STATUS_CREATED,
    OrderStage.SENT_TO_KITCHEN: f.SO_STATUS_APPROVED,
    OrderStage.OUT_FOR_DELIVERY: f.SO_STATUS_APPROVED,
    OrderStage.DELIVERED: f.SO_STATUS_DELIVERED,
    OrderStage.CANCELLED: f.SO_STATUS_CANCELLED,
    OrderStage.REFUNDED: f.SO_STATUS_CANCELLED,
}


def sales_order_status(stage) -> str | None:
    """Zoho's Sales Order Status for an order stage; None before payment."""
    try:
        return SALES_ORDER_STATUS.get(OrderStage(stage))
    except ValueError:
        return None


def sales_order_fields(order, *, product_ids: dict[str, str], zoho_contact_id: str | None,
                       delivery_at: datetime | None = None) -> dict | None:
    """A Sales Order from our Order: one grid row per dish, at the price
    charged. Delivery, Uber fees and tax go in Adjustment, so the Grand Total
    equals what the customer paid. None when no dish has a Product (Zoho
    refuses a Sales Order with an empty grid)."""
    rows, grid_total = [], Decimal("0")
    for line in order.items or []:
        parsed = f.parse_line(line)
        if parsed is None or parsed[0] not in product_ids:
            continue
        code, name, quantity, unit_price = parsed
        rows.append({f.SO_ITEM_PRODUCT: {"id": product_ids[code]},
                     f.SO_ITEM_QUANTITY: quantity,
                     f.SO_ITEM_LIST_PRICE: f.jsonable(unit_price),
                     f.SO_ITEM_DESCRIPTION: name})
        grid_total += unit_price * quantity
    if not rows:
        return None
    # Everything paid beyond the grid: delivery, Uber fees, tax (and any dish
    # that had no Product, so the Grand Total still matches the charge).
    adjustment = Decimal(str(order.total or 0)) - grid_total
    address = ", ".join(p for p in (order.delivery_address, order.apartment_unit) if p) or None
    notes = [f"Delivery slot: {order.slot_label}" if order.slot_label else "",
             f"Instructions: {order.delivery_instructions}"
             if order.delivery_instructions else "",
             "Adjustment = delivery charge, taxes and fees."]
    notes_text = "\n".join(n for n in notes if n)
    return {
        f.SO_SUBJECT: order.order_number,
        f.SO_CONTACT: {"id": zoho_contact_id} if zoho_contact_id else None,
        f.SO_STATUS: sales_order_status(order.stage) or f.SO_STATUS_CREATED,
        f.SO_DUE_DATE: delivery_at.date().isoformat() if delivery_at else None,
        f.SO_ITEMS: rows,
        f.SO_ADJUSTMENT: f.jsonable(adjustment),
        f.SO_SHIPPING_STREET: address,
        f.SO_SHIPPING_CODE: order.postal_code,
        f.SO_DESCRIPTION: notes_text or None,
    }


async def create_sales_order(fields: dict) -> str | None:
    so_id = await zoho_client.create_record(f.SALES_ORDERS, f.compact(fields))
    log.info("zoho_sales_order_created", order_number=fields.get(f.SO_SUBJECT), zoho_id=so_id)
    return so_id


async def update_sales_order(zoho_so_id: str, fields: dict) -> None:
    payload = f.compact(fields)
    if payload:
        await zoho_client.update_record(f.SALES_ORDERS, zoho_so_id, payload)


# --- Order Items: one record per dish on a paid order --------------------------
def order_item_fields(order, line: dict, *, zoho_order_id: str, zoho_product_id: str | None,
                      zoho_contact_id: str | None) -> dict | None:
    """An Order Item from one cart line; None when the line is unreadable.

    The Product link is left out (not blanked) when the dish is unknown, so
    a later push can fill it in without losing one set earlier.
    """
    parsed = f.parse_line(line)
    if parsed is None:
        return None
    code, name, quantity, unit_price = parsed
    return {
        f.I_NAME_DEFAULT: f"{order.order_number} / {name}"[:255],
        f.I_ORDER: {"id": zoho_order_id},
        f.I_PRODUCT: {"id": zoho_product_id} if zoho_product_id else None,
        f.I_CONTACT: {"id": zoho_contact_id} if zoho_contact_id else None,
        f.I_DISH_CODE: code,
        f.I_QUANTITY: quantity,
        f.I_UNIT_PRICE: unit_price,
        f.I_LINE_TOTAL: unit_price * quantity,
    }


async def create_order_item(fields: dict) -> str | None:
    item_id = await zoho_client.create_record(settings.zoho_order_items_module,
                                              f.compact(fields))
    log.info("zoho_order_item_created", name=fields.get(f.I_NAME_DEFAULT), zoho_id=item_id)
    return item_id


async def update_order_item(zoho_item_id: str, fields: dict) -> None:
    await zoho_client.update_record(settings.zoho_order_items_module, zoho_item_id,
                                    f.compact(fields))


# --- Products: one per menu dish -----------------------------------------------
def product_fields(item, *, cuisine: str | None, category: str | None) -> dict:
    """A Zoho Product from a MenuItem. Product Code is the dish's retailer_id,
    the stable id carts and orders use, so a renamed dish stays one Product."""
    photo = item.image_url if str(item.image_url or "").startswith("http") else None
    return {
        f.P_NAME: item.name,
        f.P_CODE: item.retailer_id,
        f.P_UNIT_PRICE: item.price,
        f.P_ACTIVE: bool(item.is_available),
        f.P_DESCRIPTION: item.description,
        f.P_CUISINE: cuisine,
        f.P_DISH_CATEGORY: category,
        f.P_PACK_SIZE: item.pack_size,
        f.P_SERVES: item.serves,
        f.P_PHOTO_URL: photo,
    }


async def find_product_by_code(code: str) -> dict | None:
    records = await zoho_client.search(f.PRODUCTS, f"({f.P_CODE}:equals:{code})")
    return records[0] if records else None


async def create_product(fields: dict) -> str | None:
    product_id = await _with_unique_name(
        lambda payload: zoho_client.create_record(f.PRODUCTS, payload), fields)
    log.info("zoho_product_created", code=fields.get(f.P_CODE), zoho_id=product_id)
    return product_id


async def update_product(zoho_product_id: str, fields: dict) -> None:
    await _with_unique_name(
        lambda payload: zoho_client.update_record(f.PRODUCTS, zoho_product_id, payload),
        fields)


async def _with_unique_name(write, fields: dict):
    """Write a Product; Product Name is unique in Zoho, so when another dish
    already has this name, the cuisine and category are added to it."""
    try:
        return await write(f.compact(fields))
    except IntegrationError as exc:
        if _rejected_field(exc, code="DUPLICATE_DATA") != f.P_NAME:
            raise
    suffix = ", ".join(p for p in (fields.get(f.P_CUISINE), fields.get(f.P_DISH_CATEGORY))
                       if p) or fields.get(f.P_CODE)
    unique = f"{fields[f.P_NAME]} ({suffix})"
    log.warning("zoho_product_name_taken", name=fields[f.P_NAME], using=unique)
    return await write(f.compact({**fields, f.P_NAME: unique}))


# --- Vendors: one per kitchen ------------------------------------------------
def vendor_fields(outlet) -> dict:
    """A Zoho Vendor from an Outlet. Outlet Code is how it is found again."""
    cuisines = ", ".join(c for c in (f.cuisine_label(x) for x in outlet.cuisines or []) if c)
    return {
        f.V_NAME: outlet.name,
        f.V_OUTLET_CODE: outlet.code,
        f.V_PHONE: outlet.phone,
        f.V_EMAIL: outlet.kitchen_email,
        f.V_STREET: ", ".join(p for p in (outlet.address_line1, outlet.address_line2) if p) or None,
        f.V_CITY: outlet.city,
        f.V_STATE: outlet.state,
        f.V_ZIP: outlet.postal_code,
        f.V_COUNTRY: f.country_name(outlet.country),
        # Zoho's own "Address - Latitude / Longitude" on Vendors are numbers.
        f.V_LATITUDE: float(outlet.latitude) if outlet.latitude is not None else None,
        f.V_LONGITUDE: float(outlet.longitude) if outlet.longitude is not None else None,
        f.V_KITCHEN_WHATSAPP: phone(outlet.kitchen_whatsapp) if outlet.kitchen_whatsapp else None,
        f.V_DELIVERY_RADIUS_KM: outlet.delivery_radius_km,
        f.V_DESCRIPTION: (f"Kitchen of the Shero WhatsApp ordering bot. Cuisines: {cuisines}."
                          if cuisines else "Kitchen of the Shero WhatsApp ordering bot."),
    }


async def find_vendor(code: str, name: str) -> dict | None:
    """By Outlet Code; by name for a Vendor typed in by hand."""
    criteria = f"(({f.V_OUTLET_CODE}:equals:{code})or({f.V_NAME}:equals:{name}))"
    records = await zoho_client.search(f.VENDORS, criteria)
    return records[0] if records else None


async def create_vendor(fields: dict) -> str | None:
    vendor_id = await _dropping_rejected_picklists(
        lambda payload: zoho_client.create_record(f.VENDORS, payload), fields)
    log.info("zoho_vendor_created", code=fields.get(f.V_OUTLET_CODE), zoho_id=vendor_id)
    return vendor_id


async def update_vendor(zoho_vendor_id: str, fields: dict) -> None:
    await _dropping_rejected_picklists(
        lambda payload: zoho_client.update_record(f.VENDORS, zoho_vendor_id, payload), fields)


async def _dropping_rejected_picklists(write, fields: dict):
    """Write a Vendor; State and Country are picklists whose options vary by
    org, so a value Zoho does not know is dropped rather than failing the
    whole record."""
    payload = dict(fields)
    for _ in range(len(f.VENDOR_PICKLISTS) + 1):
        try:
            return await write(f.compact(payload))
        except IntegrationError as exc:
            rejected = _rejected_field(exc)
            if rejected not in f.VENDOR_PICKLISTS or payload.get(rejected) is None:
                raise
            log.warning("zoho_vendor_picklist_value_dropped", field=rejected,
                        value=payload[rejected])
            payload[rejected] = None
    raise AssertionError("unreachable")  # pragma: no cover


def is_missing_record(exc: IntegrationError) -> bool:
    """True when Zoho refused a write because the record no longer exists."""
    return exc.status_code == 404 or _rejected_field(exc) == "id"


# --- helpers -----------------------------------------------------------------
def _rejected_field(exc: IntegrationError, *, code: str = "INVALID_DATA") -> str | None:
    """The field Zoho named in a per-record error of this code, if any.

    The payload is the record's own error (a 2xx with `status: error`
    inside) or the whole response body (a 4xx), which wraps it in `data`.
    """
    record = exc.payload if isinstance(exc.payload, dict) else {}
    wrapped = record.get("data")
    if isinstance(wrapped, list) and wrapped and isinstance(wrapped[0], dict):
        record = wrapped[0]
    if record.get("code") != code:
        return None
    return (record.get("details") or {}).get("api_name")


async def update_order_stage(zoho_order_id: str, stage: OrderStage, *,
                             at: datetime | None = None) -> None:
    """Order Status, plus the dispatch or delivery time when there is one."""
    payload: dict = {f.O_STATUS: str(stage)}
    if stage == OrderStage.OUT_FOR_DELIVERY:
        payload[f.O_DISPATCHED_TIME] = f.zoho_datetime(at)
    if stage == OrderStage.DELIVERED:
        payload[f.O_DELIVERED_TIME] = f.zoho_datetime(at)
    await zoho_client.update_record(settings.zoho_orders_module, zoho_order_id,
                                    f.compact(payload))
    log.info("zoho_order_stage_updated", zoho_id=zoho_order_id, stage=str(stage))


def _coord(value) -> str | None:
    return None if value is None else f"{float(value):.6f}"


def _missing_mandatory(exc: IntegrationError, api_name: str) -> bool:
    """True when Zoho refused a write for lack of this required field."""
    return _rejected_field(exc, code="MANDATORY_NOT_FOUND") == api_name
