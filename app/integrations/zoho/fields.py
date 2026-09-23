"""Zoho field API names, in one place.

Standard Zoho fields are fixed. The CUSTOM_* names must match fields the
client's Zoho administrator creates (the spec lists this under "Approval to
create custom fields, stages and an Orders module"). If the admin names them
differently, change them here only - nothing else in the codebase hardcodes a
Zoho field name.

See docs/zoho-setup.md for the exact list to hand to the admin.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

# --- Standard Zoho fields ----------------------------------------------------
LAST_NAME = "Last_Name"      # required by Zoho on Leads and Contacts
FIRST_NAME = "First_Name"
COMPANY = "Company"          # required by Zoho on Leads
EMAIL = "Email"
PHONE = "Phone"
MOBILE = "Mobile"
STREET = "Street"
CITY = "City"
STATE = "State"
ZIP_CODE = "Zip_Code"
LEAD_SOURCE = "Lead_Source"
LEAD_STATUS = "Lead_Status"  # holds the bot stage (New Enquiry ... Converted)
DESCRIPTION = "Description"

# --- Custom fields on Lead / Contact -----------------------------------------
CUSTOM_WHATSAPP_NUMBER = "WhatsApp_Number"
CUSTOM_SELECTED_OUTLET = "Selected_Outlet"
CUSTOM_DISTANCE_KM = "Distance_KM"
CUSTOM_CUISINE_PREFERENCE = "Cuisine_Preference"
CUSTOM_AD_ID = "Ad_ID"
CUSTOM_CAMPAIGN_ID = "Campaign_ID"
CUSTOM_APARTMENT_UNIT = "Apartment_Unit"
CUSTOM_STAGE_TIMESTAMPS = "Stage_Timestamps"   # multi-line text, JSON blob

# --- Custom fields on the Orders module --------------------------------------
ORDER_NUMBER = "Name"          # the module's primary field holds the order id
ORDER_CONTACT = "Contact_Name" # lookup -> Contacts
ORDER_OUTLET = "Outlet"
ORDER_ITEMS = "Items"          # multi-line text: "2 x Chicken Biryani"
ORDER_DISH_TOTAL = "Dish_Total"
ORDER_DELIVERY_CHARGE = "Delivery_Charge"
ORDER_TAX = "Tax"
ORDER_TOTAL = "Total"
ORDER_SLOT = "Delivery_Slot"
ORDER_STRIPE_PAYMENT_ID = "Stripe_Payment_ID"
ORDER_STAGE = "Order_Stage"
ORDER_DELIVERED_TIME = "Delivered_Time"
ORDER_DELIVERY_ADDRESS = "Delivery_Address"

# Company is mandatory on Zoho Leads but meaningless for a consumer food
# order, so every lead carries this constant.
DEFAULT_COMPANY = "Shero Home Food - WhatsApp"


def split_name(full_name: str | None) -> tuple[str, str]:
    """Split a free-text name into (first, last).

    Zoho rejects a Lead without Last_Name, so a single-word name is used as the
    last name and an empty name falls back to a placeholder.
    """
    cleaned = (full_name or "").strip()
    if not cleaned:
        return "", "WhatsApp Customer"
    parts = cleaned.split()
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]


def jsonable(value: Any) -> Any:
    """Coerce Decimal/None into something Zoho's JSON API accepts."""
    if isinstance(value, Decimal):
        return float(value)
    return value


def compact(fields: dict[str, Any]) -> dict[str, Any]:
    """Drop None values so a partial update never blanks an existing field."""
    return {k: jsonable(v) for k, v in fields.items() if v is not None}
