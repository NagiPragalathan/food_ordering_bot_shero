"""What the bot adds to a Zoho CRM, as Zoho's settings APIs want it.

Used by scripts/setup_zoho_crm.py, which compares this against the live CRM
and creates only what is missing: the Orders and Order Items modules, the
fields below (on those two and on Leads, Contacts, Products and Vendors), and
the Lead Source and Lead Status options. Zoho derives
each field's API name from its label ("Bot Stage" -> "Bot_Stage"); fields.py
holds those names, and a test checks the two agree.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.db.models.enums import LeadStage, OrderStage
from app.integrations.zoho import fields as f

# The bot's own modules, by the spec's names; the live names are
# ZOHO_ORDERS_MODULE and ZOHO_ORDER_ITEMS_MODULE.
ORDERS = "Orders"
ORDER_ITEMS = "Order_Items"


@dataclass(frozen=True)
class FieldSpec:
    module: str
    api_name: str          # what Zoho will call it; checked after creation
    body: dict             # one entry of the create API's "fields" list


def _text(label: str, length: int = 255) -> dict:
    return {"field_label": label, "data_type": "text", "length": length}


def _textarea(label: str, length: int = 2000) -> dict:
    # Zoho accepts only 2000, 32000 or 50000 for a multi-line field.
    assert length in (2000, 32000, 50000), length
    return {"field_label": label, "data_type": "textarea", "length": length,
            "textarea": {"type": "small" if length <= 2000 else "large"}}


def _money(label: str) -> dict:
    return {"field_label": label, "data_type": "currency", "length": 16, "decimal_place": 2}


def _datetime(label: str) -> dict:
    return {"field_label": label, "data_type": "datetime"}


def _picklist(label: str, options: list[str]) -> dict:
    return {"field_label": label, "data_type": "picklist",
            "pick_list_values": [{"display_value": o, "actual_value": o} for o in options]}


def _lookup(label: str, module: str, related_list: str) -> dict:
    # display_label names the related list shown on the looked-up record.
    return {"field_label": label, "data_type": "lookup",
            "lookup": {"module": {"api_name": module}, "display_label": related_list}}


# The spec's Lead / Contact data (section 2) that Zoho has no field for.
# Created on both modules under the same names, so the values carry over
# when a Lead is converted.
PERSON_FIELDS: tuple[tuple[str, dict], ...] = (
    (f.ADDRESS_LINE_2, _text("Address Line 2", 120)),
    (f.LATITUDE, _text("Latitude", 20)),
    (f.LONGITUDE, _text("Longitude", 20)),
    (f.AD_ID, _text("Ad ID", 80)),
    (f.CAMPAIGN_ID, _text("Campaign ID", 80)),
    (f.CUISINE, _text("Cuisine Preference", 60)),
    (f.BOT_STAGE, _picklist("Bot Stage", [s.value for s in LeadStage])),
    (f.BOT_STAGE_HISTORY, _textarea("Bot Stage History")),
    (f.SELECTED_OUTLET, _text("Selected Outlet", 120)),
    (f.DISTANCE_KM, {"field_label": "Distance KM", "data_type": "double",
                     "length": 8, "decimal_place": 2}),
)

# The spec's Order data (section 2). Amounts are plain numbers in the CRM's
# currency field; the bot charges in STRIPE_CURRENCY.
ORDER_FIELDS: tuple[tuple[str, dict], ...] = (
    (f.O_CONTACT, _lookup("Contact", f.CONTACTS, "Orders")),
    (f.O_LEAD, _lookup("Lead", f.LEADS, "Orders")),
    (f.O_CUSTOMER_NO, {"field_label": "Customer No", "data_type": "phone", "length": 30}),
    (f.O_CHANNEL, _picklist("Channel", [f.CHANNEL_WHATSAPP_BOT])),
    (f.O_STATUS, _picklist("Order Status", [s.value for s in OrderStage])),
    (f.O_ADDRESS, _text("Address", 255)),
    (f.O_LATITUDE, _text("Latitude", 20)),
    (f.O_LONGITUDE, _text("Longitude", 20)),
    (f.O_CUISINE, _text("Cuisine", 60)),
    (f.O_OUTLET, _lookup("Outlet", f.VENDORS, "Orders")),
    (f.O_OUTLET_NAME, _text("Outlet Name", 120)),
    (f.O_DELIVERY_SLOT, _text("Delivery Slot", 80)),
    (f.O_DELIVERY_TIME, _datetime("Delivery Time")),
    (f.O_ITEMS, _textarea("Items")),
    (f.O_DISH_TOTAL, _money("Dish Total")),
    (f.O_DELIVERY_CHARGE, _money("Delivery Charge")),
    (f.O_TAXES_AND_FEES, _money("Taxes and Fees")),
    (f.O_ORDER_TOTAL, _money("Order Total")),
    (f.O_STRIPE_PAYMENT_ID, _text("Stripe Payment ID", 255)),
    (f.O_INSTRUCTIONS, _textarea("Delivery Instructions")),
    (f.O_PLACED_TIME, _datetime("Order Placed Time")),
    (f.O_PAID_TIME, _datetime("Paid Time")),
    (f.O_DISPATCHED_TIME, _datetime("Dispatched Time")),
    (f.O_DELIVERED_TIME, _datetime("Delivered Time")),
)

# One record per dish on a paid order, in the bot's Order Items module. The
# three lookups give the Order, the Product and the Contact an "Order Items"
# related list; the copied dish code, name and price survive a Product being
# deleted or renamed.
ORDER_ITEM_FIELDS: tuple[tuple[str, dict], ...] = (
    (f.I_ORDER, _lookup("Order", ORDERS, "Order Items")),
    (f.I_PRODUCT, _lookup("Product", f.PRODUCTS, "Order Items")),
    (f.I_CONTACT, _lookup("Contact", f.CONTACTS, "Order Items")),
    (f.I_DISH_CODE, _text("Dish Code", 120)),
    (f.I_QUANTITY, {"field_label": "Quantity", "data_type": "integer", "length": 9}),
    (f.I_UNIT_PRICE, _money("Unit Price")),
    (f.I_LINE_TOTAL, _money("Line Total")),
)

# What a menu dish has that Zoho's Products module does not.
PRODUCT_FIELDS: tuple[tuple[str, dict], ...] = (
    (f.P_CUISINE, _text("Cuisine", 60)),
    (f.P_DISH_CATEGORY, _text("Dish Category", 120)),
    (f.P_PACK_SIZE, _text("Pack Size", 80)),
    (f.P_SERVES, _text("Serves", 80)),
    (f.P_PHOTO_URL, {"field_label": "Photo URL", "data_type": "website"}),
)

# What a kitchen has that Zoho's Vendors module does not (it has an address
# with Latitude and Longitude of its own).
VENDOR_FIELDS: tuple[tuple[str, dict], ...] = (
    (f.V_OUTLET_CODE, _text("Outlet Code", 40)),
    (f.V_KITCHEN_WHATSAPP, {"field_label": "Kitchen WhatsApp", "data_type": "phone",
                            "length": 30}),
    (f.V_DELIVERY_RADIUS_KM, {"field_label": "Delivery Radius KM", "data_type": "double",
                              "length": 8, "decimal_place": 2}),
)

FIELDS: tuple[FieldSpec, ...] = (
    tuple(FieldSpec(module, api_name, body)
          for module in f.PERSON_MODULES for api_name, body in PERSON_FIELDS)
    + tuple(FieldSpec(ORDERS, api_name, body) for api_name, body in ORDER_FIELDS)
    + tuple(FieldSpec(ORDER_ITEMS, api_name, body) for api_name, body in ORDER_ITEM_FIELDS)
    + tuple(FieldSpec(f.PRODUCTS, api_name, body) for api_name, body in PRODUCT_FIELDS)
    + tuple(FieldSpec(f.VENDORS, api_name, body) for api_name, body in VENDOR_FIELDS)
)

# Options added to existing picklists: (module, field api name, option).
# Lead Status gets the funnel stages so Zoho's own stage bar shows them.
PICKLIST_OPTIONS: tuple[tuple[str, str, str], ...] = tuple(
    (module, f.LEAD_SOURCE, option)
    for module in f.PERSON_MODULES for option in f.LEAD_SOURCES
) + tuple((f.LEADS, f.LEAD_STATUS, s.value) for s in LeadStage)

# The bot's own modules, for Zoho's create-module API. Each gets a "Name"
# display field: the order number, or "order number / dish" on an item.
MODULES: dict[str, dict[str, str]] = {
    ORDERS: {"singular_label": "Order", "plural_label": "Orders"},
    ORDER_ITEMS: {"singular_label": "Order Item", "plural_label": "Order Items"},
}
ORDERS_MODULE = MODULES[ORDERS]


def api_name_for(label: str) -> str:
    """How Zoho names a field from its label."""
    return label.replace(" ", "_")


def missing_fields(existing: dict[str, set[str]]) -> list[FieldSpec]:
    """Specs whose field is not in `existing` ({module: {api names}})."""
    return [spec for spec in FIELDS
            if spec.api_name not in existing.get(spec.module, set())]
