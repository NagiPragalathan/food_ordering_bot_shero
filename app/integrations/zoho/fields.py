"""Zoho field API names, in one place.

The bot uses Zoho the way the spec describes (section 2), on a CRM of its own:

  * a food customer is a **Lead** until their first payment, when Zoho's own
    Lead-to-Contact conversion makes them a **Contact**
  * every paid order is a record in the bot's **Orders** custom module,
    linked to the Contact (or to the Lead, if the conversion failed)

Fields marked "added by the bot" do not exist until `scripts/setup_zoho_crm.py
--apply` creates them (their definitions are in schema.py). Nothing else in the
codebase hardcodes a Zoho field name. See docs/zoho-setup.md for the mapping.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

LEADS = "Leads"
CONTACTS = "Contacts"
PERSON_MODULES = (LEADS, CONTACTS)

# --- Leads and Contacts: Zoho's own fields -----------------------------------
FIRST_NAME = "First_Name"
LAST_NAME = "Last_Name"               # required by Zoho on both modules
PHONE = "Phone"
MOBILE = "Mobile"                     # searched too: a hand-entered record may use it
EMAIL = "Email"
LEAD_SOURCE = "Lead_Source"
LEAD_STATUS = "Lead_Status"           # Leads only: mirrors Bot Stage, so Zoho's stage bar and reports show the funnel
COMPANY = "Company"                   # Leads only; never set: a Lead with one becomes an Account on conversion

# The address block is named differently on the two modules.
ADDRESS_FIELDS: dict[str, dict[str, str]] = {
    LEADS: {"street": "Street", "city": "City", "state": "State", "zip": "Zip_Code"},
    CONTACTS: {"street": "Mailing_Street", "city": "Mailing_City",
               "state": "Mailing_State", "zip": "Mailing_Zip"},
}

# Added by the bot, with the same names on Leads and Contacts.
ADDRESS_LINE_2 = "Address_Line_2"
LATITUDE = "Latitude"
LONGITUDE = "Longitude"
AD_ID = "Ad_ID"
CAMPAIGN_ID = "Campaign_ID"
CUISINE = "Cuisine_Preference"
BOT_STAGE = "Bot_Stage"                       # picklist: the 11 funnel stages
BOT_STAGE_HISTORY = "Bot_Stage_History"       # multi-line: one "time  stage" per line
SELECTED_OUTLET = "Selected_Outlet"
DISTANCE_KM = "Distance_KM"
WHATSAPP_PROFILE_NAME = "WhatsApp_Profile_Name"  # the name on their WhatsApp profile; never used as their name

# Lead Source options the bot adds; Zoho's defaults have neither. They equal
# the bot's own `Customer.lead_source` values.
LEAD_SOURCE_WHATSAPP = "WhatsApp"
LEAD_SOURCE_META_AD = "Meta Ad"
LEAD_SOURCES = (LEAD_SOURCE_WHATSAPP, LEAD_SOURCE_META_AD)

# --- Orders: the bot's own module (every field added by the bot) ---------------
# The module's display field ("Name" unless the module was made by hand with
# another one - see ZOHO_ORDERS_NAME_FIELD) holds the order number.
O_NAME_DEFAULT = "Name"
O_CONTACT = "Contact"                 # lookup -> Contacts: the customer
O_LEAD = "Lead"                       # lookup -> Leads: only if conversion failed
O_CUSTOMER_NO = "Customer_No"
O_CHANNEL = "Channel"
O_STATUS = "Order_Status"             # picklist: the bot's order stages, verbatim
O_ADDRESS = "Address"
O_LATITUDE = "Latitude"
O_LONGITUDE = "Longitude"
O_CUISINE = "Cuisine"
O_OUTLET = "Outlet"                   # lookup -> Vendors: the kitchen that cooked it
O_OUTLET_NAME = "Outlet_Name"         # kept as text too, for orders filed before the lookup existed
O_DELIVERY_SLOT = "Delivery_Slot"
O_DELIVERY_TIME = "Delivery_Time"     # start of the delivery slot, for date filters and reports
O_SALES_ORDER = "Sales_Order"         # lookup -> Sales_Orders: the same order with its product grid
O_ITEMS = "Items"                     # multi-line: one "qty x dish @ price = total" per line
O_DISH_TOTAL = "Dish_Total"
O_DELIVERY_CHARGE = "Delivery_Charge"
O_TAXES_AND_FEES = "Taxes_and_Fees"
O_ORDER_TOTAL = "Order_Total"
O_STRIPE_PAYMENT_ID = "Stripe_Payment_ID"
O_INSTRUCTIONS = "Delivery_Instructions"
O_PLACED_TIME = "Order_Placed_Time"
O_PAID_TIME = "Paid_Time"
O_DISPATCHED_TIME = "Dispatched_Time"
O_DELIVERED_TIME = "Delivered_Time"

CHANNEL_WHATSAPP_BOT = "WhatsApp Bot"

# --- Order Items: the bot's own module, one record per dish on a paid order ---
# Linked to the Order, the Product and the Contact, so each of those shows an
# "Order Items" related list, and Zoho reports can count dishes sold.
I_NAME_DEFAULT = "Name"               # display field: "SHO-260928-ABCDE / Sambar"
I_ORDER = "Order"                     # lookup -> Orders
I_PRODUCT = "Product"                 # lookup -> Products; empty if the dish left the menu
I_CONTACT = "Contact"                 # lookup -> Contacts; set once the customer is one
I_DISH_CODE = "Dish_Code"             # the bot's dish id (MenuItem.retailer_id)
I_QUANTITY = "Quantity"
I_UNIT_PRICE = "Unit_Price"
I_LINE_TOTAL = "Line_Total"

# --- Sales Orders: Zoho's own module, one per paid order -------------------------
# The order again, as Zoho's native order with a product grid (like a Deal's
# or a Quote's): one row per dish with quantity and price, delivery and fees
# as the Adjustment, so its Grand Total is what the customer paid.
SALES_ORDERS = "Sales_Orders"
SO_SUBJECT = "Subject"                # required: the order number
SO_CONTACT = "Contact_Name"           # lookup -> Contacts
SO_STATUS = "Status"                  # Created / Approved / Delivered / Cancelled
SO_DUE_DATE = "Due_Date"              # delivery day
SO_ITEMS = "Ordered_Items"            # the product grid (subform)
SO_ITEM_PRODUCT = "Product_Name"      # lookup -> Products
SO_ITEM_QUANTITY = "Quantity"
SO_ITEM_LIST_PRICE = "List_Price"     # the price charged, not the Product's current one
SO_ITEM_DESCRIPTION = "Description"
SO_ADJUSTMENT = "Adjustment"
SO_DESCRIPTION = "Description"
SO_SHIPPING_STREET = "Shipping_Street"
SO_SHIPPING_CODE = "Shipping_Code"
SO_STATUS_CREATED = "Created"
SO_STATUS_APPROVED = "Approved"
SO_STATUS_DELIVERED = "Delivered"
SO_STATUS_CANCELLED = "Cancelled"

# --- Products: Zoho's own module, one per menu dish ---------------------------
PRODUCTS = "Products"
P_NAME = "Product_Name"               # unique in Zoho (case-insensitive)
P_CODE = "Product_Code"               # the bot's dish id (MenuItem.retailer_id): how a dish is found again
P_UNIT_PRICE = "Unit_Price"
P_ACTIVE = "Product_Active"           # false when the dish is hidden from the menu
P_DESCRIPTION = "Description"
# Added by the bot.
P_CUISINE = "Cuisine"
P_DISH_CATEGORY = "Dish_Category"
P_PACK_SIZE = "Pack_Size"
P_SERVES = "Serves"
P_PHOTO_URL = "Photo_URL"

# --- Vendors: Zoho's own module, one per kitchen (the bot's outlets) ----------
VENDORS = "Vendors"
V_NAME = "Vendor_Name"
V_PHONE = "Phone"
V_EMAIL = "Email"
V_STREET = "Street"
V_CITY = "City"
V_STATE = "State"                     # picklist in Zoho: dropped if the org lacks the value
V_ZIP = "Zip_Code"
V_COUNTRY = "Country"                 # picklist of country names, see country_name()
V_LATITUDE = "Latitude"               # Zoho's "Address - Latitude", a number (unlike the bot's text fields elsewhere)
V_LONGITUDE = "Longitude"
V_DESCRIPTION = "Description"
# Added by the bot.
V_OUTLET_CODE = "Outlet_Code"         # the bot's outlet code: how a kitchen is found again
V_KITCHEN_WHATSAPP = "Kitchen_WhatsApp"
V_DELIVERY_RADIUS_KM = "Delivery_Radius_KM"
VENDOR_PICKLISTS = (V_STATE, V_COUNTRY)

COUNTRY_NAMES = {
    "US": "United States", "IN": "India", "GB": "United Kingdom", "CA": "Canada",
    "AU": "Australia", "AE": "United Arab Emirates", "SG": "Singapore",
}

NAME_PLACEHOLDER = "WhatsApp Customer"        # Last Name until the customer gives one


def country_name(code: str | None) -> str | None:
    """"US" (the bot's outlet country) -> "United States", Zoho's picklist value."""
    cleaned = (code or "").strip().upper()
    return COUNTRY_NAMES.get(cleaned, cleaned or None)


def parse_line(line: dict) -> tuple[str, str, int, Decimal] | None:
    """A cart line -> (dish code, name, quantity, unit price); None if unreadable."""
    try:
        quantity = int(line.get("quantity") or 1)
        unit_price = Decimal(str(line.get("unit_price") or "0"))
    except (TypeError, ValueError, ArithmeticError):
        return None
    name = str(line.get("name") or line.get("retailer_id") or "Item")
    code = str(line.get("retailer_id") or line.get("id") or name)
    return code, name, quantity, unit_price


def split_name(full_name: str | None) -> tuple[str, str]:
    """Split a free-text name into (first, last).

    A single-word name is the last name; an empty one falls back to a
    placeholder, since Zoho's Last Name is required.
    """
    cleaned = (full_name or "").strip()
    if not cleaned:
        return "", NAME_PLACEHOLDER
    parts = cleaned.split()
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]


def join_name(record: dict) -> str:
    """The name on a Zoho record, or "" for the placeholder."""
    name = " ".join(p for p in (record.get(FIRST_NAME), record.get(LAST_NAME)) if p)
    return "" if name.strip() == NAME_PLACEHOLDER else name.strip()


def lead_source_option(value: str | None) -> str:
    """The Lead Source option for the bot's lead_source; WhatsApp when unknown."""
    return value if value in LEAD_SOURCES else LEAD_SOURCE_WHATSAPP


def cuisine_label(cuisine: str | None) -> str | None:
    """"north-indian" (the bot's slug) -> "North Indian"."""
    cleaned = (cuisine or "").strip()
    return cleaned.replace("-", " ").replace("_", " ").title() if cleaned else None


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
