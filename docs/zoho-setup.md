Written for: the Shero Zoho CRM administrator.

# Zoho CRM setup

Everything the bot needs in Zoho. Shero's CRM is on **zoho.in**.

The bot writes each food customer as a **Lead** and each paid order to the
**Orders** module, linked to the Lead. It never converts a Lead and never
writes to **Contacts**, which are Kitchen Partners in this CRM. Its writes do
not set off Zoho workflows.

Every Zoho field name the bot uses is in one file,
`app/integrations/zoho/fields.py`.

---

## 1. API credentials

**You do not need to generate a refresh token by hand.** The dashboard does
the OAuth exchange: Admin → Settings → Zoho CRM → **Connect Zoho**.

### Create the client

1. Go to the API console for **your data centre** — `api-console.zoho.com`,
   `.in`, `.eu`, `.au`, `.jp` or `.ca`. It must match the domain you log in at.
2. Click **Add Client** → **Server-based Applications**.
3. Fill it in:

   | Field | Value |
   |---|---|
   | Client Name | `Shero Ordering Bot` |
   | Homepage URL | your dashboard root, e.g. `https://api.<yourdomain>` |
   | Authorized Redirect URIs | `https://api.<yourdomain>/admin/settings/zoho/callback` |

   The redirect URI must match **character for character**, including the
   scheme and any trailing path: `PUBLIC_BASE_URL` followed by
   `/admin/settings/zoho/callback`. For the test tunnel that is
   `https://shero-order-bot.loca.lt/admin/settings/zoho/callback`.

4. Click **CREATE** and note the **Client ID** and **Client Secret**. Put them
   in `.env` as `ZOHO_CLIENT_ID` and `ZOHO_CLIENT_SECRET`.
5. On the client's **Settings** tab, turn on **Multi-DC** and enable every data
   centre. Connecting always starts at `accounts.zoho.com`; with Multi-DC on,
   an account on `.in`, `.eu` and so on can sign in there too.

### Connect

Admin → Settings → Zoho CRM has a **domain** dropdown (zoho.com, zoho.in, ...)
and **Connect Zoho**. Pick the domain your CRM opens on and the app was
created on; you are sent to that Zoho domain to approve access. Zoho's reply says which data centre the account is on
(`location=in`, `us`, `eu`, ...), and the bot stores that as
`ZOHO_DATA_CENTER` with the refresh token, encrypted. Nobody picks the data
centre, and there is nothing to copy back or email. Only known `location`
values are accepted, because the client secret is sent to that data centre's
accounts host.

Scopes requested: `ZohoCRM.modules.ALL`, `ZohoCRM.settings.ALL`,
`ZohoCRM.users.READ`.

> **Local testing.** Against a dev server, use `http://localhost:8000` as the
> Homepage URL and
> `http://localhost:8000/admin/settings/zoho/callback` as the redirect URI.
> Zoho accepts `http` for `localhost` only; everything else must be `https`.

> **Reconnecting.** The flow forces the consent screen every time, because
> Zoho returns a refresh token only on first consent. If it ever reports that
> no refresh token came back, remove the app under Zoho Accounts → Connected
> Apps and connect again.

> The refresh token does not expire, but Zoho limits how often it can mint new
> access tokens (~15 per 10 minutes). The bot caches tokens, so this is only a
> concern if many instances run at once.

## 2. Fields the bot adds

`python -m scripts.setup_zoho_crm` lists what is missing, and `--apply` creates
it. It only ever adds: nothing is renamed or deleted, and running it again is
safe. The definitions are in `app/integrations/zoho/schema.py`. Created on
28 Sep 2026:

| Module | Field | Type | Holds |
|---|---|---|---|
| Leads | Bot Stage (`Bot_Stage`) | Picklist, the 11 stages below | Where the customer is in the ordering funnel |
| Leads | Bot Stage History (`Bot_Stage_History`) | Multi-line | One line per stage: `2026-09-28 10:04 UTC  Cart Created` |
| Leads | Selected Outlet (`Selected_Outlet`) | Single line | Kitchen that serves them |
| Leads | Distance KM (`Distance_KM`) | Decimal | Distance to that kitchen |
| Orders | Lead (`Lead`) | Lookup to Leads | The customer; shows as an "Orders" list on the Lead |
| Orders | Outlet Name (`Outlet_Name`) | Single line | Kitchen name |
| Orders | Delivery Slot (`Delivery_Slot`) | Single line | e.g. "Fri 25 Sep, 7:00 PM - 8:00 PM" |
| Orders | Delivery Charge (`Delivery_Charge`) | Currency | Uber delivery fee |
| Orders | Taxes and Fees (`Taxes_and_Fees`) | Currency | Uber's extra fees plus tax |
| Orders | Order Total (`Order_Total`) | Currency | What the customer paid |
| Orders | Stripe Payment ID (`Stripe_Payment_ID`) | Single line | Stripe payment reference |
| Orders | Delivered Time (`Delivered_Time`) | Date/Time | When it was delivered |
| Orders | Channel option **WhatsApp Bot** | Picklist option | Tells bot orders apart in reports |

Bot Stage values (spec section 2): New Enquiry, Details Captured, Cuisine
Selected, Cart Created, Not Serviceable, Outlet Selected, Slot Selected,
Payment Link Sent, Payment Abandoned, Payment Failed, Converted.

The bot's own stage has its own field rather than **Lead Status**, which the
kitchen-partner team uses for its applicants.

> Six fields were also added to the **Customers** module on 28 Sep, before
> the bot moved to Leads (Bot Stage, Bot Stage History, Selected Outlet,
> Distance KM, Cuisine Preference, Ad ID). The bot no longer uses them; they
> are empty and can be deleted in Setup.

> **Order Total vs Grand Total.** The Orders module's own `Grand_Total` is a
> formula over the dish lines, so it leaves out the delivery charge and fees.
> `Order_Total` is the amount actually charged.

## 3. What goes where

**Leads** (one per WhatsApp number):

| Zoho field (label) | From the bot | When |
|---|---|---|
| First Name / Last Name | Name the customer gave ("WhatsApp Customer" until then) | First message, then the name step |
| Contact Number (`Phone`) | WhatsApp number, `+` and digits | First message |
| Email | Email the customer gave | Email step |
| Lead Source | `Whatsapp` | First message |
| Facebook Ad ID / Facebook Ad Campaign ID | Click-to-WhatsApp ad and campaign | First message |
| Address (`Street`), Address Second Line, PinCode (`Zip_Code`), Latitude, Longitude | The delivery address | As soon as an address is saved, and again when delivery is checked |
| Cuisine (`Native_Cuisine_at_home`) | Cuisine of the dishes picked (Chettinad, Andhra, Kerala) | When a dish from a new cuisine is added |
| Selected Outlet, Distance KM | Kitchen that delivers, and how far | Delivery checked |
| Bot Stage, Bot Stage History | Funnel stage | Every stage change |

**Orders** (one per paid order):

| Zoho field | From the bot |
|---|---|
| Order No | Our order number, e.g. `SHO-260928-ABCDE` |
| Lead | The customer's Lead |
| Customer No | Contact number |
| Channel | `WhatsApp Bot` |
| Order Status | See the mapping below |
| Address, Latitude, Longitude | Delivery address and pin |
| Cuisine | Customer's cuisine |
| Item Details | One row per dish: name (`SAP_Name`), quantity, unit price |
| Order Instructions | Delivery instructions |
| Order Placed Time / Order Accepted Time | Order created / paid |
| Order Dispatched Time / Delivered Time | Out for delivery / delivered |
| Outlet Name, Delivery Slot, Delivery Charge, Taxes and Fees, Order Total, Stripe Payment ID | See section 2 |

Our order stages -> the existing **Order Status** options:

| Bot order stage | Order Status |
|---|---|
| Paid & Slot Booked, Sent to Kitchen | Confirmed |
| Out for Delivery | Dispatched |
| Delivered | Completed |
| Cancelled, Refunded | Cancelled |

## 4. How records flow

```
First message      -> Lead (found by phone, else created), Bot Stage New Enquiry
Each step          -> Bot Stage + Bot Stage History; details as they are captured
Payment succeeds   -> Bot Stage Converted; Order created and linked to the Lead
Delivery updates   -> Order Status (and dispatch / delivered time) updated
```

The spec's "Lead -> Contact" step becomes **Bot Stage = Converted**: in this
CRM a Contact is a Kitchen Partner, so the Lead is not converted.

Returning customers are matched on Phone, so a number gets one Lead and every
paid order is linked to it. Zoho's search takes about a minute to find a newly
created record; the bot keeps the Lead's id after creating it, so this only
matters if the bot's own database is wiped.

Orders are filed **when paid**, as the spec says. Unpaid and changed orders
stay in the bot's own dashboard, not in Zoho's Orders.

## 5. Suggested workflows

Not required by the bot, and not set off by it (the bot's writes skip
workflows). A workflow on a **field update** of Bot Stage, run by a person or
a schedule, can still use:

- **Payment Abandoned**: assign a follow-up task.
- **Not Serviceable**: tag for an area expansion list.
- **Payment Failed**: alert an agent to reach out.

## 6. Verifying it works

```bash
python -m scripts.setup_zoho_crm     # "Nothing to do" = every field is there
```

Then send one WhatsApp message to the bot: a Lead appears with Bot Stage
`New Enquiry`. If it does not, the application log names the reason: look for
`zoho_ensure_lead_failed` or `zoho_lead_update_failed`.

The bot is built so Zoho failures never block a customer. A missing record
means a configuration problem to fix, not a lost order.
