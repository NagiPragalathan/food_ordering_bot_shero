Written for: the Shero Zoho CRM administrator.

# Zoho CRM setup

Everything the bot needs configured in Zoho, in the order it should be done.
Nothing here requires a developer — it is all Setup screens.

If you name anything differently, that is fine: tell us the API names and we
change one file (`app/integrations/zoho/fields.py`). Nothing else in the
codebase hardcodes a Zoho field name.

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
   scheme and any trailing path. The Settings page prints the exact value to
   paste, so copy it from there rather than typing it.

4. Click **CREATE** and note the **Client ID** and **Client Secret**.

### Connect

In Admin → Settings → Zoho CRM, paste the Client ID and Client Secret, pick
your **data centre** from the dropdown, and press **Connect Zoho**. You are
sent to Zoho to approve access, and the refresh token is stored encrypted —
there is nothing to copy back and nothing to email.

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

## 2. Lead Status picklist

The bot writes the eleven spec stages into the standard **Lead Status** field.

Setup → Customization → Modules → **Leads** → Lead Status → add these values
**exactly as written**:

```
New Enquiry
Details Captured
Cuisine Selected
Cart Created
Not Serviceable
Outlet Selected
Slot Selected
Payment Link Sent
Payment Abandoned
Payment Failed
Converted
```

Using the standard field rather than a custom one means Zoho's built-in lead
funnel and conversion reports work with no extra configuration.

## 3. Custom fields on Leads **and** Contacts

Add each to both modules, so the data survives conversion.

| Field label | Type | API name | Holds |
|---|---|---|---|
| WhatsApp Number | Single Line | `WhatsApp_Number` | The number that messaged us |
| Selected Outlet | Single Line | `Selected_Outlet` | Chosen kitchen name |
| Distance KM | Decimal | `Distance_KM` | Distance to that kitchen |
| Cuisine Preference | Single Line | `Cuisine_Preference` | Last cuisine chosen |
| Ad ID | Single Line | `Ad_ID` | Click-to-WhatsApp ad |
| Campaign ID | Single Line | `Campaign_ID` | Click-to-WhatsApp campaign |
| Apartment Unit | Single Line | `Apartment_Unit` | Apartment / unit / floor |
| Stage Timestamps | **Multi Line** | `Stage_Timestamps` | JSON: when each stage was reached |

**About Stage Timestamps.** The spec asks for a timestamp per stage change so
reports show where and when customers drop off. Rather than eleven date
fields cluttering the layout, it is stored as one JSON blob:

```json
{"New Enquiry": "2026-09-22T10:00:00+00:00",
 "Details Captured": "2026-09-22T10:00:41+00:00"}
```

If you would rather have eleven individually reportable datetime fields,
**tell us before you build this** — it is a small change now and a migration
later.

## 4. The Orders module

Setup → Modules and Fields → **Create New Module**, named `Orders`.

| Field label | Type | API name |
|---|---|---|
| Order ID | Single Line (**primary field**) | `Name` |
| Contact Name | **Lookup → Contacts** | `Contact_Name` |
| Outlet | Single Line | `Outlet` |
| Items | Multi Line | `Items` |
| Dish Total | Currency | `Dish_Total` |
| Delivery Charge | Currency | `Delivery_Charge` |
| Tax | Currency | `Tax` |
| Total | Currency | `Total` |
| Delivery Slot | Single Line | `Delivery_Slot` |
| Delivery Address | Multi Line | `Delivery_Address` |
| Stripe Payment ID | Single Line | `Stripe_Payment_ID` |
| Order Stage | **Picklist** | `Order_Stage` |
| Delivered Time | Date/Time | `Delivered_Time` |

`Order_Stage` picklist values:

```
Pending Payment
Paid & Slot Booked
Sent to Kitchen
Out for Delivery
Delivered
Cancelled
Refunded
```

> `Delivery_Charge` carries the Uber delivery fee **plus** Uber's extra fees
> as a single figure, matching what the customer saw on the summary. `Tax` is
> only our own sales tax, if you charge one.

## 5. How records flow

```
First message      -> Lead created (upsert on Phone, so never duplicated)
Each step          -> Lead Status + Stage Timestamps updated
Payment succeeds   -> Lead converted to Contact, Order created under it
Delivery updates   -> Order Stage updated
```

Returning customers are matched on the WhatsApp number in the standard `Phone`
field. An existing Contact means a new Order is filed against them and no
second Lead is created.

**Deals are deliberately not created on conversion.** Orders live in the
Orders module instead, which is what the spec describes.

## 6. Suggested workflows

Not required by the bot, but the spec mentions re-engagement:

- **Abandoned payment** — Lead Status becomes `Payment Abandoned`: assign a
  follow-up task, or trigger the `payment_expired` template from Gallabox.
- **Not serviceable** — Lead Status becomes `Not Serviceable`: tag for an area
  expansion list.
- **Payment failed** — alert an agent to reach out.

## 7. Verifying it works

Once the credentials are in `.env`:

```bash
python -m scripts.check_config      # confirms nothing is missing
curl localhost:8000/health/readiness
```

Then send one WhatsApp message to the bot and confirm a Lead appears with
Lead Status `New Enquiry`. If it does not, the application log will name the
reason — look for `zoho_ensure_lead_failed`.

Remember: the bot is built so Zoho failures never block a customer. A missing
Lead means a configuration problem to fix, not a lost order.
