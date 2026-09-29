Written for: the Shero client contact and whoever administers their Meta, Zoho, Stripe, Uber and AWS accounts.

# What we need from you to go live

Everything below maps to a variable in `.env`. Nothing is hardcoded, so as
soon as a value arrives it can be dropped in and that integration starts
working — you do not have to collect all of it before we can test parts of it.

**Never send secrets over WhatsApp, email or chat.** Use a password manager
share (1Password / Bitwarden), or add our email as a user on each platform and
we will generate the keys ourselves.

Legend: 🔴 blocks go-live · 🟡 needed before that area works · ⚪ optional

---

## 1. Gallabox (WhatsApp) 🔴

| What | Where to find it | `.env` variable |
|---|---|---|
| API Key | Settings → API Keys (**app.gallabox.com/apikey**) | `GALLABOX_API_KEY` |
| API Secret | same screen, **shown only once** | `GALLABOX_API_SECRET` |
| Channel ID | Settings → WhatsApp Channel → Channel Id | `GALLABOX_CHANNEL_ID` |
| Account ID | the 24-hex id in any dashboard URL, `/accounts/<this>/…` | `GALLABOX_ACCOUNT_ID` |
| Admin login for our team | invite our email as Admin | — |

### Creating the API key and secret

1. Log in to Gallabox as an **admin** and go to
   **[app.gallabox.com/apikey](https://app.gallabox.com/apikey)** (or Settings
   → API Keys in the left nav).
2. Click **Add New**.
3. Name it `shero-ordering-bot`.
4. Set **Permission** to **All**.
5. Complete the **two-factor step** when prompted.
6. Click **Add API Key**.

**On the permission scope.** The dialog offers **All**, **Read only** and
**Restricted**, and defaults to Read only. The bot sends messages, assigns
conversations for the "Talk to Us" handover, and looks up returning contacts,
so **Read only cannot work** — it blocks every send.

If your security policy requires **Restricted** rather than All, tick at
minimum:

| Group | Permissions |
|---|---|
| Contact | `read`, `readMany` |
| Message | `create` (or `send`) |
| Conversation | `read`, `update` (or `assign`) — this is the agent handover |

Two things to avoid:

- **Do not grant `delete`, `import` or `export` on Contact.** The bot never
  uses them, and a key that can bulk-export the contact list is a liability
  well beyond what this integration needs.
- **`not-trigger-bot`, `not-re-assign` and `not-add-tag` are restrictions, not
  grants.** Leave them unchecked — `not-trigger-bot` in particular could stop
  bot flows firing.

A scope that is almost right fails as a 403 partway through a real customer's
order, so verify it with `--send` (below) before go-live. If that returns 403,
widen the scope to All.

> **Copy the API Secret immediately.** Gallabox shows it once and never again.
> If it is lost, delete the key and create a new one. The API Key ID stays
> visible in the API Keys tab afterwards, so only the secret is at risk.

### Finding the Channel ID

Settings → **WhatsApp Channel** → **Channel Id** (on some accounts it is under
Settings → Connect → WhatsApp Channel).

It is a 24-character hex string, **not a phone number** — it looks like
`647062b51e3c77d2741188cb`. This is the single most common mix-up, so there is
a check for it (below).

### Verifying before you send them to us

Once the three values are in `.env`:

```bash
python -m scripts.check_gallabox                     # format checks, offline
python -m scripts.check_gallabox --send 17325550142  # real test message
```

The first catches blank values, the secret pasted into the key field, and a
phone number in the channel ID field. The second proves all three work
together by sending a real WhatsApp message.

> For the `--send` test, message the business number from that phone **first**,
> then run it straight away. A free-form WhatsApp text only reaches someone who
> has contacted the business in the last 24 hours — outside that window it
> needs an approved template.

Also confirm, in writing:

- **The connected WhatsApp number** that customers will message.
- **That your plan includes API access, webhooks and the bot builder.** If it
  does not, the bot cannot receive messages at all.
- **Where to configure the inbound webhook.** We will give you a URL like
  `https://api.<yourdomain>/webhooks/gallabox` and a token to paste alongside
  it (`GALLABOX_WEBHOOK_TOKEN` — we generate this, you just paste it).
- **How your workspace does agent handover**, so "Talk to Us" lands in the
  right inbox.

> One thing we will verify together on day one: the exact JSON shape Gallabox
> posts to our webhook. Our parser already handles the documented shapes and
> degrades safely on anything else, but a single real test message from your
> account confirms it in minutes.

## 2. Meta / Facebook 🔴

The menu now comes from your sheet, not the Meta catalogue, so Meta is no
longer needed to *browse* dishes. It is still needed for two things that do
block go-live:

| What | Why we need it | `.env` variable |
|---|---|---|
| WhatsApp Business Account access | **approving the 10 templates** | — |
| Business verification status | template approval, messaging limits | — |
| Business Portfolio admin access | managing WhatsApp and ads | — |
| Ad account access | Click-to-WhatsApp campaign attribution | — |

Optional, only if you later want the native WhatsApp cart:

| What | `.env` variable |
|---|---|
| Commerce Manager access + Catalogue ID | `META_CATALOG_ID` |
| System user token (`catalog_management`) | `META_SYSTEM_USER_TOKEN` |

> **Your messaging limit currently reads 0.25K.** If that is the standard
> WhatsApp tier-1 cap, it is 250 unique customers per rolling 24 hours, which
> a food business will reach quickly. It tiers up automatically on good-quality
> sending. Worth planning launch volume around.

Your channel already shows **FB Business Verification: Verified** and
**Quality Rating: Green**, so templates should approve without trouble.

## 3. Zoho CRM 🔴

| What | Where | `.env` variable |
|---|---|---|
| Client ID | api-console.zoho.in, signed in to the bot's own Zoho account → Server-based app | `ZOHO_CLIENT_ID` |
| Client Secret | same | `ZOHO_CLIENT_SECRET` |
| Admin user (or our email added as Administrator) | | — |
| Your Zoho edition/plan | determines API call limits | — |

Required OAuth scopes: `ZohoCRM.modules.ALL`, `ZohoCRM.settings.ALL`,
`ZohoCRM.users.READ`, `ZohoCRM.org.READ`.

**You do not need to produce a refresh token.** Create a *Server-based
Application* in Zoho's API console, register the redirect URI, turn on
Multi-DC, then press **Connect Zoho** in Settings and approve access. The data
centre (`.com`, `.in`, ...) is detected from your login. Full steps in
[zoho-setup.md](zoho-setup.md).

**We also need your approval to create** the Orders module, the custom fields
and the Lead Source options listed in
[zoho-setup.md](zoho-setup.md). That document is written so your Zoho admin
can action it directly.

## 4. Stripe 🔴

| What | Where | `.env` variable |
|---|---|---|
| Test secret key | Developers → API keys | `STRIPE_SECRET_KEY` |
| Live secret key (at go-live) | same | `STRIPE_SECRET_KEY` |
| Publishable key | same | `STRIPE_PUBLISHABLE_KEY` |
| Our email added as a **Developer** team member | Settings → Team | — |
| Account activated for live payments | | — |

**Until a key is set, payments are mocked:** ordering and the WhatsApp
summary still work end to end, and Pay Now opens a "Test payment" page
(`/pay/mock/<order>`) instead of Stripe. Nothing is charged. Its **Simulate
successful payment** button runs what Stripe's webhook would (slot booked,
customer marked Converted and the Order filed in Zoho, kitchen told), so the whole
flow can be tested. The button is refused once a Stripe key is set.

We create the webhook endpoint ourselves and will give you the resulting
`whsec_…` to store (`STRIPE_WEBHOOK_SECRET`).

Please also confirm:

- **Currency** (the spec shows `$`, so we have assumed USD).
- **Tax treatment** — is sales tax included in the dish prices, or added on
  top? If added, at what rate? (`TAX_PERCENT`, currently `0`.)
- **Refund policy** — full refund on outlet cancellation is what we have built.

## 5. Uber Direct 🔴

| What | `.env` variable |
|---|---|
| Customer ID | `UBER_CUSTOMER_ID` |
| Client ID | `UBER_CLIENT_ID` |
| Client Secret | `UBER_CLIENT_SECRET` |

Plus the **pickup address of every outlet** — street, city, state, ZIP. These
go into the outlet records and are sent to Uber when quoting a delivery.

## 6. Kitchen and delivery area 🔴

**No file needed — this is set up in the admin dashboard** (Settings →
Kitchen & delivery area). We need these details from you:

- **Kitchen address** — the pickup address sent to Uber
- **Latitude and longitude** — right-click the kitchen in Google Maps and copy
  the coordinates. These drive the delivery-area check *and* the Uber pickup,
  so approximate values give approximate answers.
- **Who you deliver to** — either a radius in km, a list of ZIP codes, or
  both. A ZIP list is usually the honest choice: it says exactly where a
  driver will go rather than drawing a circle over a river.
- **Kitchen WhatsApp number** for the new-paid-order alert
- **Operating hours** per weekday — delivery slots are generated only inside
  these windows, so a kitchen with none set cannot take an order. Set them in
  the same form (Settings → Kitchen → Opening hours), along with the slot
  length and how many orders you can handle per slot.

## 7. Menu 🟢 done

**Already imported** from your Google Sheet: 3 cuisines (Chettinad, Kerala,
Andhra), 55 categories, 287 dishes. That also settles the spec's open question
about the third cuisine — it is Chettinad.

Customers are charged the **MRP** column. **PPP** is stored as cost and shown
only as a margin figure in the admin dashboard; it never reaches a customer,
a Stripe charge or a Zoho record.

To update the menu later: edit the sheet, then Admin → Import → paste the URL.
Re-importing updates prices in place rather than duplicating rows. Keep the
sheet shared as "Anyone with the link can view".

Still outstanding for the menu:

## 7b. Dish images 🟢 done

All **287 dishes now have their photo**, taken from your sheet.

Worth recording why this looked impossible at first: a CSV export of the sheet
shows an empty IMAGE column, because the pictures are anchored *over* the
cells rather than stored *in* them. The XLSX export does carry them, so that
is what the importer reads, matching each picture to its dish by row.

Editing a photo in the sheet and re-importing updates it.

## 8. Backend APIs you may already have 🟡

The spec lists these as ours to confirm. **We have built working versions of
both**, so nothing is blocked — but if you already have them, say so and we
will switch to yours with a config change (`OUTLET_SOURCE=remote`,
`SLOT_SOURCE=remote`) rather than duplicating your data.

| API | What we built | If yours exists |
|---|---|---|
| Delivery slots | generated from your operating hours, with capacity and holds | send us the URL and response shape |
| Payment page | Stripe Checkout | tell us and we will reuse it |

(The nearby-restaurants API is no longer needed now there is one kitchen.)

## 9. AWS, domain and email 🔴

| What | Why | `.env` variable |
|---|---|---|
| IAM user (not root) + preferred region | hosting | — |
| DNS access for a subdomain | webhooks and the payment redirect | — |
| Gmail access | server/notification proxy | — |

Two subdomains, both pointing at the service:

- `api.<yourdomain>` → webhooks and the ops API (`PUBLIC_BASE_URL`)
- `pay.<yourdomain>` → the short Pay Now redirect (`PAY_REDIRECT_BASE_URL`)

The `pay` subdomain exists because Stripe checkout URLs are far too long for a
WhatsApp button.

## 10. Optional ⚪

| What | What you gain | `.env` variable |
|---|---|---|
| Google Maps API key | accurate addresses from map pins and address search, ZIP geocoding and real road distances. Enable the **Geocoding API**. Without it we fall back to OpenStreetMap, which is free but rate-limited, often lacks house numbers and has no SLA | `GOOGLE_MAPS_API_KEY` |
| Google Maps browser key | draws Google's map (with satellite view) on the ordering page. It is visible in the page, so create a **separate** key restricted to the **Maps JavaScript API** and to your domain as HTTP referrer | `GOOGLE_MAPS_BROWSER_KEY` |

Step-by-step guide for creating both keys, with click paths and direct links: [google-maps-api-key-guide.pdf](google-maps-api-key-guide.pdf).

---

## Quickest path to a working demo

Gallabox is done and the menu is loaded. These three unlock a complete test
order end to end:

1. **Stripe test keys** — so payment links can be created
2. **Uber Direct credentials** — so a delivery charge can be quoted
3. **The kitchen address + coordinates + delivery area** — entered in
   Admin → Settings

Meta is no longer on the critical path: the menu comes from your sheet, not
the Meta catalogue. Meta credentials are still needed for **template
approval** before go-live, and for Click-to-WhatsApp ad attribution.

Zoho can come last — the bot is built so that a CRM outage or missing
credentials never blocks a customer from ordering. It logs the failure and
carries on.

---

## Decisions we still need from you

These are genuine gaps in the spec rather than missing credentials. See
[open-questions.md](open-questions.md) for the full list, but the two that
affect build work are:

1. **How the kitchen gets notified** of a new paid order (spec step 17). The
   spec lists no approved template for this, and a WhatsApp message to a
   kitchen that has not messaged us first falls outside Meta's 24-hour window.
   Options: an 11th template, email, or the admin Orders screen alone. We have
   built it so the order is always visible in the dashboard regardless.
2. **Tax and currency** — we have assumed USD with tax already included in
   your MRP prices. Correct us if not.
