Written for: the Shero Zoho CRM administrator.

# Zoho CRM setup

Everything the bot needs in Zoho. The bot has a Zoho CRM of its own (a fresh
org on **zoho.in**), separate from the kitchen-partner CRM; see section 7 for
what was left behind there.

Every Zoho field name the bot uses is in one file,
`app/integrations/zoho/fields.py`; what the setup script creates is in
`app/integrations/zoho/schema.py`.

---

## 1. How records flow

The spec (section 2), as the bot does it:

```
First message      -> Lead (found by phone in Contacts, then Leads; else created)
                      Bot Stage = New Enquiry
Each step          -> Bot Stage + Bot Stage History; details as they are captured
Payment succeeds   -> the Lead is converted to a Contact (Zoho's own conversion)
                      Bot Stage = Converted; an Order is filed under the Contact,
                      linked to the kitchen's Vendor, with one Order Item per
                      dish linked to the dish's Product
Later orders       -> Bot Stage moves on the Contact; each paid order filed under it
Delivery updates   -> the Order's Order Status (and dispatch / delivered time)
Menu edits         -> the dish's Product (Admin -> Menu -> Sync to Zoho does them all)
```

Zoho has modules for the things an order is made of, so the bot links to
them instead of copying names into text:

| The bot's | Zoho module | Matched by |
|---|---|---|
| Customer | **Leads**, then **Contacts** | Phone / Mobile |
| Paid order | **Orders** (custom, created by the setup script) | Order number |
| Dish on an order | **Order Items** (custom, created by the setup script) | Remembered per order line |
| Menu dish | **Products** (Zoho's own) | Product Code = the dish's `retailer_id` |
| Kitchen (outlet) | **Vendors** (Zoho's own) | Outlet Code = the outlet's code, or its name |

A Contact, an Order and a Product each show an **Order Items** related list;
a Vendor shows its **Orders**. Delivery slots and addresses have no Zoho
module: the slot is on the Order as text and as a Delivery Time, the address
as fields on the Order and on the Contact.

- **One record per WhatsApp number.** Returning customers are matched on
  Phone or Mobile (with or without the `+`), Contacts first. Somebody who
  paid before is found as a Contact and greeted by name, even if the bot's
  own database was cleared.
- **Conversion happens once**, on the first payment. Zoho retires the Lead;
  the Contact keeps every bot field (they have the same names on both
  modules, and the bot writes them to the Contact right after converting,
  so no conversion-mapping setup is needed). If Zoho refuses the
  conversion, the customer stays a Lead with Bot Stage Converted and the
  order is linked to the Lead; the admin's **Push to Zoho** retries it and
  moves such orders to the Contact.
- **No Account is created**, because the bot never sets Company on a Lead;
  Zoho's API does not require it, whatever the layout says.
- **Orders are filed when paid**, as the spec says. Unpaid and changed orders
  stay in the bot's own dashboard, not in Zoho.
- The bot's creates and updates do not set off Zoho workflows. A conversion
  cannot skip them, so workflows on Contacts run for each new customer.

## 2. Connect the CRM

**You do not need to generate a refresh token by hand.** The dashboard does
the OAuth exchange: Admin → Settings → Zoho CRM → pick **zoho.in** → **Connect
Zoho**.

### Create the API client (in the new account)

Sign in to the **new** Zoho account first: the client must belong to the org
the bot writes to.

1. Open the API console for the data centre: **api-console.zoho.in**.
2. Click **Add Client** → **Server-based Applications**.
3. Fill it in:

   | Field | Value |
   |---|---|
   | Client Name | `Shero Ordering Bot` |
   | Homepage URL | your dashboard root, e.g. `https://api.<yourdomain>` |
   | Authorized Redirect URIs | `https://api.<yourdomain>/admin/settings/zoho/callback` |

   The redirect URI must match **character for character**: `PUBLIC_BASE_URL`
   followed by `/admin/settings/zoho/callback`. For the test tunnel that is
   `https://shero-order-bot.loca.lt/admin/settings/zoho/callback`.

4. Click **CREATE** and note the **Client ID** and **Client Secret**. Enter
   them in Admin → Settings (Zoho card) or in `.env` as `ZOHO_CLIENT_ID` and
   `ZOHO_CLIENT_SECRET`.
5. On the client's **Settings** tab, turn on **Multi-DC** and enable every
   data centre.

### Connect

Admin → Settings → Zoho CRM: choose **zoho.in** and press **Connect Zoho**;
approve access while signed in to the new account. Zoho's reply says which
data centre the account is on, and the bot stores that as `ZOHO_DATA_CENTER`
with the refresh token, encrypted. Connecting replaces the bot's previous
connection; nothing changes in the previously connected CRM. The bot records
which org it is connected to (`ZOHO_ORG_ID`); connecting a **different** org
drops the record ids it saved for the old one, since they mean nothing there,
and the Customers page's Push to Zoho recreates the records.

Scopes requested: `ZohoCRM.modules.ALL`, `ZohoCRM.settings.ALL`,
`ZohoCRM.users.READ`, `ZohoCRM.org.READ`.

> **Reconnecting.** The flow forces the consent screen every time, because
> Zoho returns a refresh token only on first consent. If it ever reports that
> no refresh token came back, remove the app under Zoho Accounts → Connected
> Apps and connect again.

## 3. Prepare the CRM

```bash
python -m scripts.setup_zoho_crm           # dry run: lists what is missing
python -m scripts.setup_zoho_crm --apply   # creates it
```

It only ever adds - nothing is renamed or deleted - and running it again is
safe. On a fresh org it creates:

| What | Details |
|---|---|
| The **Orders** module | Custom module, plural *Orders*, singular *Order*; its display field **Name** holds the order number |
| The **Order Items** module | Custom module, plural *Order Items*, singular *Order Item*; its display field **Name** holds `order number / dish` |
| 10 fields on **Leads** and the same 10 on **Contacts** | See the table below |
| 24 fields on **Orders** | See the table below |
| 7 fields on **Order Items** | See the table below |
| 5 fields on **Products** and 3 on **Vendors** | See the tables below; the rest of a dish or kitchen goes in Zoho's own fields |
| **Lead Source** options `WhatsApp` and `Meta Ad` | On Leads and Contacts |
| **Lead Status** options: the 11 Bot Stage values | So Zoho's own stage bar on a Lead, and its Leads-by-Status reports, show the funnel |

If Zoho will not create a module through the API, the script prints the
two-minute manual step and can be run again afterwards.
Custom modules need a paid edition (a trial counts); on the free edition the
Orders and Order Items modules cannot exist, and orders are logged as not
filed. Products and Vendors are Zoho's own modules and exist in every edition.

Fields on **Leads and Contacts**:

| Field (API name) | Type | Holds |
|---|---|---|
| Address Line 2 (`Address_Line_2`) | Single line | Flat / unit |
| Latitude, Longitude | Single line | The delivery pin |
| Ad ID (`Ad_ID`), Campaign ID (`Campaign_ID`) | Single line | Click-to-WhatsApp ad and campaign |
| Cuisine Preference (`Cuisine_Preference`) | Single line | e.g. `Chettinad`, `North Indian` |
| Bot Stage (`Bot_Stage`) | Picklist, the 11 stages | Where the customer is in the ordering funnel |
| Bot Stage History (`Bot_Stage_History`) | Multi-line | One line per stage: `2026-09-28 10:04 UTC  Cart Created` |
| Selected Outlet (`Selected_Outlet`) | Single line | Kitchen that serves them |
| Distance KM (`Distance_KM`) | Decimal | Distance to that kitchen |

Bot Stage values: New Enquiry, Details Captured, Cuisine Selected, Cart
Created, Not Serviceable, Outlet Selected, Slot Selected, Payment Link Sent,
Payment Abandoned, Payment Failed, Converted.

Fields on **Orders**:

| Field (API name) | Type | Holds |
|---|---|---|
| Name | Display field | Order number, e.g. `SHO-260928-ABCDE` |
| Contact (`Contact`) | Lookup to Contacts | The customer; shows as an "Orders" list on the Contact |
| Lead (`Lead`) | Lookup to Leads | Only when the conversion failed |
| Customer No (`Customer_No`) | Phone | Contact number for the delivery |
| Channel | Picklist | `WhatsApp Bot` |
| Order Status (`Order_Status`) | Picklist | The bot's order stages, verbatim (below) |
| Address, Latitude, Longitude | Single line | Where it was delivered |
| Cuisine, Outlet Name, Delivery Slot | Single line | e.g. `Fri 25 Sep, 7:00 PM - 8:00 PM` |
| Outlet (`Outlet`) | Lookup to Vendors | The kitchen that cooked it; shows as an "Orders" list on the Vendor |
| Delivery Time (`Delivery_Time`) | Date/Time | Start of the delivery slot, for date filters |
| Items | Multi-line | One dish per line: `2 x Sambar @ 9.50 = 19.00` (the Order Items records are the structured version) |
| Dish Total, Delivery Charge, Taxes and Fees, Order Total | Currency | Amounts as charged (in `STRIPE_CURRENCY`) |
| Stripe Payment ID | Single line | Stripe payment reference |
| Delivery Instructions | Multi-line | What the customer typed |
| Order Placed Time, Paid Time, Dispatched Time, Delivered Time | Date/Time | When each happened |

Order Status values: Pending Payment, Paid & Slot Booked, Sent to Kitchen,
Out for Delivery, Delivered, Cancelled, Refunded. The bot sets Paid & Slot
Booked on payment, then the delivery stages as the kitchen advances the order,
and Cancelled or Refunded from a refund.

> If the Orders module was created by hand with a display field other than
> Name, set `ZOHO_ORDERS_NAME_FIELD` to its API name; the dry run says so.

Fields on **Order Items** (one record per dish on a paid order):

| Field (API name) | Type | Holds |
|---|---|---|
| Name | Display field | `SHO-260928-ABCDE / Sambar` |
| Order (`Order`) | Lookup to Orders | The order; shows as an "Order Items" list on it |
| Product (`Product`) | Lookup to Products | The dish; empty only if the dish is no longer on the menu |
| Contact (`Contact`) | Lookup to Contacts | The customer; shows as an "Order Items" list on the Contact |
| Dish Code (`Dish_Code`) | Single line | The dish's `retailer_id` |
| Quantity, Unit Price, Line Total | Number, Currency | As ordered |

Fields on **Products** (Zoho's own module; one per menu dish). The bot fills
Zoho's Product Name, Product Code (the dish's `retailer_id`), Unit Price,
Product Active (hidden dishes are inactive) and Description, plus:

| Field (API name) | Type | Holds |
|---|---|---|
| Cuisine, Dish Category (`Dish_Category`) | Single line | Where the dish sits on the menu |
| Pack Size (`Pack_Size`), Serves | Single line | From the sheet |
| Photo URL (`Photo_URL`) | URL | The dish photo, when it has a public URL |

Product Name is unique in Zoho. Two dishes with the same name in different
categories get the second one named `Sambar (Kerala, Curries)`.

Fields on **Vendors** (Zoho's own module; one per kitchen). The bot fills
Zoho's Vendor Name, Phone, Email, Street, City, State, Zip Code, Country,
Address - Latitude / Longitude (the kitchen's pin) and Description, plus:

| Field (API name) | Type | Holds |
|---|---|---|
| Outlet Code (`Outlet_Code`) | Single line | The outlet's code in Settings |
| Kitchen WhatsApp (`Kitchen_WhatsApp`) | Phone | Where kitchen alerts go |
| Delivery Radius KM (`Delivery_Radius_KM`) | Decimal | Delivery area |

State and Country are picklists in Zoho; a value the org's lists do not
have is left blank rather than failing the record.

## 4. What goes where

**Leads, then Contacts** (one per WhatsApp number):

| Zoho field | From the bot | When |
|---|---|---|
| First Name / Last Name | Name the customer gave (`WhatsApp Customer` until then) | First message, then the name step |
| Phone | WhatsApp number, `+` and digits | First message |
| Email | Email the customer gave | Email step |
| Lead Source | `WhatsApp`, or `Meta Ad` from a click-to-WhatsApp ad | First message |
| Ad ID / Campaign ID | The ad and campaign | First message |
| Street (`Mailing Street` on a Contact), Address Line 2, Zip Code (`Mailing Zip`), Latitude, Longitude | The delivery address | As soon as an address is saved, and again when delivery is checked |
| Cuisine Preference | Cuisine of the dishes picked | When a dish from a new cuisine is added |
| Selected Outlet, Distance KM | Kitchen that delivers, and how far | Delivery checked |
| Bot Stage, Bot Stage History | Funnel stage | Every stage change |
| Lead Status (Leads only) | The same stage, so Zoho's stage bar shows it | Every stage change |

On conversion the Contact receives all of the above plus Bot Stage Converted,
and the address the paid order went to.

**Orders, Order Items, Products and Vendors**:

| Zoho record | From the bot | When |
|---|---|---|
| Order | The paid order: totals, address, slot, Stripe reference, status, kitchen (Vendor link), delivery time | Payment succeeds; status on each kitchen step |
| Order Item | Each cart line: dish (Product link), code, quantity, unit price, line total, the customer (Contact link) | Right after the Order |
| Product | Each dish ordered (created or brought up to date), and every dish on **Sync to Zoho** or an admin edit | Before its Order Item; Admin → Menu |
| Vendor | The kitchen that cooked the order, and every kitchen on **Sync to Zoho** | Before the Order; Admin → Menu |

Products and Vendors are matched by code, so a dish renamed on the menu
stays one Product, and a Product or Vendor deleted in Zoho is recreated on
the next order or sync. Nothing is ever deleted in Zoho by the bot.

## 5. Suggested workflows

Not required by the bot. A workflow on a **field update** of Bot Stage can
use:

- **Payment Abandoned**: assign a follow-up task or send the re-engagement
  template.
- **Not Serviceable**: tag for an area-expansion list.
- **Payment Failed**: alert an agent to reach out.

Workflows on **Contact created** run for every converted customer - keep
them to what a food customer should get.

## 6. Verifying it works

```bash
python -m scripts.setup_zoho_crm     # "Every field and option is already there"
```

Then:

1. Admin → Menu → **Sync to Zoho**: every dish appears in Products and every
   kitchen in Vendors.
2. Send one WhatsApp message to the bot: a Lead appears with Bot Stage
   `New Enquiry`.
3. Order and pay (test card): the Lead disappears from Leads, a Contact
   appears with Bot Stage `Converted`, an Order appears under it with the
   kitchen in Outlet, and the Order's **Order Items** list has one row per
   dish, each pointing at its Product.
4. Admin → Orders → advance the order: its Order Status follows.

If a record is missing, the application log names the reason:
`zoho_ensure_record_failed`, `zoho_record_update_failed`,
`zoho_lead_convert_failed`, `zoho_order_create_failed`,
`zoho_order_item_failed`, `zoho_product_sync_failed` or
`zoho_vendor_sync_failed`. Admin → Customers → **Push to Zoho** resends
everything for one customer once the cause is fixed, including the Order
Items and kitchen link of orders filed earlier.

The bot is built so Zoho failures never block a customer. A missing record
means a configuration problem to fix, not a lost order.

## 7. The previously connected CRM

Until 29 Sep 2026 the bot wrote to the kitchen-partner CRM. Fields it added
there are empty and can be deleted in that org's Setup: on **Leads** Bot
Stage, Bot Stage History, Selected Outlet, Distance KM; on **Orders** Lead,
Outlet Name, Delivery Slot, Delivery Charge, Taxes and Fees, Order Total,
Stripe Payment ID, Delivered Time and the Channel option `WhatsApp Bot`; on
**Customers** Bot Stage, Bot Stage History, Selected Outlet, Distance KM,
Cuisine Preference, Ad ID. The bot no longer touches that org.
