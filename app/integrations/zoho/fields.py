"""Zoho field API names, in one place.

Shero's Zoho CRM (zoho.in): food customers are **Leads** and their paid
orders go in the **Orders** module. Contacts there are Kitchen Partners, so
the bot never converts a Lead or writes a Contact.

Fields marked "added by the bot" do not exist until `scripts/setup_zoho_crm.py
--apply` creates them (their definitions are in schema.py). Nothing else in
the codebase hardcodes a Zoho field name.

See docs/zoho-setup.md for the full mapping.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

# --- Leads module ------------------------------------------------------------
LEADS = "Leads"

L_FIRST_NAME = "First_Name"
L_LAST_NAME = "Last_Name"             # required by Zoho
L_PHONE = "Phone"                     # labelled "Contact Number"
L_EMAIL = "Email"
L_STREET = "Street"                   # labelled "Address"
L_ADDRESS_LINE_2 = "Address_Second_Line"
L_CITY = "City"
L_STATE = "State"
L_ZIP = "Zip_Code"                    # labelled "PinCode"
L_LATITUDE = "Latitude"
L_LONGITUDE = "Longitude"
L_LEAD_SOURCE = "Lead_Source"
L_CUISINE = "Native_Cuisine_at_home"  # labelled "Cuisine"; picklist, see CUISINES
L_AD_ID = "Facebook_Ad_ID"
L_CAMPAIGN_ID = "Facebook_Ad_Campaign_ID"
# Added by the bot.
L_BOT_STAGE = "Bot_Stage"                     # picklist: the 11 funnel stages
L_BOT_STAGE_HISTORY = "Bot_Stage_History"     # multi-line: one "time  stage" per line
L_SELECTED_OUTLET = "Selected_Outlet"
L_DISTANCE_KM = "Distance_KM"

LEAD_SOURCE_WHATSAPP = "Whatsapp"             # an existing Lead_Source option
# Options of the Cuisine picklist; a cuisine outside it is not written.
CUISINES = ("Chettinad", "Andhra", "Kerala", "North Indian", "Multi Cuisines",
            "My Bowl", "Rice Express", "Curry Home")

# --- Orders module -----------------------------------------------------------
O_LEAD = "Lead"                       # lookup -> Leads (added by the bot)
O_CUSTOMER_NO = "Customer_No"
O_ORDER_NO = "Order_No"
O_ADDRESS = "Address"
O_CITY = "City"
O_STATE = "State"
O_LATITUDE = "Latitude"
O_LONGITUDE = "Longitude"
O_CUISINE = "Cuisine"
O_STATUS = "Order_Status"
O_CHANNEL = "Channel"
O_PLACED_TIME = "Order_Placed_Time"
O_ACCEPTED_TIME = "Order_Accepted_Time"       # = paid
O_DISPATCHED_TIME = "Order_Dispatched_Time"
O_INSTRUCTIONS = "Order_Instruction_s"
O_ITEMS = "Item_Details"              # subform, see I_* below
# Added by the bot.
O_OUTLET_NAME = "Outlet_Name"
O_DELIVERY_SLOT = "Delivery_Slot"
O_DELIVERY_CHARGE = "Delivery_Charge"
O_TAXES_AND_FEES = "Taxes_and_Fees"
O_ORDER_TOTAL = "Order_Total"         # the charged total; Grand_Total is a formula over the items only
O_STRIPE_PAYMENT_ID = "Stripe_Payment_ID"
O_DELIVERED_TIME = "Delivered_Time"

CHANNEL_WHATSAPP_BOT = "WhatsApp Bot"         # option added to Channel by the bot

# Item_Details subform rows.
I_NAME = "SAP_Name"
I_QUANTITY = "Quantity"
I_UNIT_PRICE = "Unit_Price"


def split_name(full_name: str | None) -> tuple[str, str]:
    """Split a free-text name into (first, last).

    A single-word name is the last name; an empty one falls back to a
    placeholder, since Zoho's name fields are required.
    """
    cleaned = (full_name or "").strip()
    if not cleaned:
        return "", "WhatsApp Customer"
    parts = cleaned.split()
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]


def cuisine_option(cuisine: str | None) -> str | None:
    """The Cuisine picklist option matching `cuisine`, or None."""
    wanted = (cuisine or "").strip().lower()
    return next((c for c in CUISINES if c.lower() == wanted), None)


def zoho_datetime(value: datetime | None) -> str | None:
    """Zoho wants `2026-09-28T10:00:00+00:00`: no microseconds."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def jsonable(value: Any) -> Any:
    """Coerce Decimal into something Zoho's JSON API accepts."""
    if isinstance(value, Decimal):
        return float(value)
    return value


def compact(fields: dict[str, Any]) -> dict[str, Any]:
    """Drop None values so a partial update never blanks an existing field."""
    return {k: jsonable(v) for k, v in fields.items() if v is not None}
