Written for: the team that runs this server and the engineers who change it.

# DST India: the second business on the bot

DST India is Shero's Indian WhatsApp number (Gallabox channel **DST INDIA**,
+91 93600 56005). It runs **the same ordering bot as Shero US**, as a complete
and separate business on the same server:

|  | Shero US | DST India |
|---|---|---|
| Admin pages | `/admin/` | `/dst/admin/`, the **DST India** tab at the top of every page |
| Database | `data/shero.db` | `data/shero_dst.db` (its own, beside it) |
| WhatsApp number | its channel (`GALLABOX_CHANNEL_ID`) | the DST INDIA channel (`DST_GALLABOX_CHANNEL_ID`) |
| Payments | Stripe | **Razorpay** payment links |
| Delivery | Uber Direct (quote and courier) | **its own delivery staff**: a flat charge, free above an amount |
| Money, distance, address | $, miles, ZIP code, +1 | ₹, km, PIN code, +91, Asia/Kolkata |
| WhatsApp templates | `payment_link`, … | `dst_payment_link`, … (same messages, in ₹) |
| Order numbers | `SHO-261116-K4T9P` | `DST-261116-K4T9P` |
| CRM | Zoho | **its own Zoho CRM**, connected on DST India → Settings (zoho.in, zoho.com, …) |

Every page Shero US has, DST India has, with the same names: Dashboard, Menu,
Orders, Offers, Loyalty, Nudges, Kitchens, Payouts, Customers, Bot replies,
Settings. The one difference is the delivery page: Shero US's **Uber queue**
is DST India's **Delivery queue**, the list its riders work from. A tab keeps
you in the same section (Orders → Orders).

Nothing of one business is read or written while serving the other: menus,
kitchens, customers, orders, offers, loyalty, nudges, payouts and every
setting saved on the admin pages are in that business's own database. Only
the code, the Gallabox account and the admin logins are shared.

## How it works

`app/core/tenants.py` holds which business is being served, set once at the
edge and read everywhere else:

- **A web request** by its path: everything under `/dst` is DST India
  (admin pages, ordering links, the Pay Now redirect, webhooks, `/dst/health`).
- **A WhatsApp message** by the Gallabox channel it came in on. Both numbers
  are in one Gallabox account, which sends every message to the one webhook;
  `gallabox/channel.tenant_for` sends each to its own business.
- **A scheduled job** runs separately for each business: Shero US's jobs keep
  their names, DST India's are their own jobs (`dst_nudges`, …), so a slow or
  failing DST run never delays or fails a Shero US one.

Shero US is never affected by DST India: a mistyped `DST_` variable leaves
DST India switched off (logged as `business_settings_invalid`, by name only)
while Shero US runs on, and a failed DST migration does not stop the server
from starting. `tests/services/test_us_unaffected_by_dst.py` holds this.

`settings` is then that business's settings (`app/core/config.py`:
`Settings` for Shero US, `DstSettings` for DST India), and a database session
is a session on its database (`app/db/session.py`). Token and template caches
are kept per business. Admin logins live in Shero US's database and work for
both.

Money, distances, postal codes and phone numbers go through
`app/core/locale.py`, so the same page reads "$12.50 · 3 miles · ZIP code"
for Shero US and "₹249.00 · 5 km · PIN code" for DST India.

## Setting it up

1. **Variables** (`.env`, see the DST section of `.env.example`):

   ```
   DST_GALLABOX_CHANNEL_ID=6af30a65a7aa4cd16b22569a
   DST_WHATSAPP_BUSINESS_NUMBER=919360056005
   DST_RAZORPAY_KEY_ID=rzp_live_...
   DST_RAZORPAY_KEY_SECRET=...
   DST_RAZORPAY_WEBHOOK_SECRET=...
   ```

   The Gallabox API key, webhook token, admin secrets and Google Maps keys are
   shared with Shero US. `python -m scripts.check_config` lists what each
   business is still missing.

2. **Database.** On the server the deploy migrates both databases
   (`deploy/docker-compose.prod.yml`). By hand:
   `alembic -x tenant=dst upgrade head`. With a SQLite `DATABASE_URL` the DST
   file is made beside it; any other database needs `DST_DATABASE_URL`.

3. **Razorpay webhook.** Razorpay Dashboard → Webhooks → add
   `https://<domain>/dst/webhooks/razorpay`, event `payment_link.paid`, and
   put its secret in `DST_RAZORPAY_WEBHOOK_SECRET`.
   DST India → Settings shows the keys' state and the exact URL.

4. **WhatsApp templates.** DST India's copies of the ordering messages
   (order summary, payment link, confirmation, kitchen alert, …) are named
   `dst_<name>`, priced in ₹, and must be approved on the DST INDIA channel:
   `python -m scripts.submit_templates --tenant dst` shows them,
   `--submit` sends them to Meta. Until one is approved, the bot sends the
   same words as an ordinary message inside the 24-hour window.

5. **Menu.** DST India's menu comes from the Shero ERP's item exports, one
   Excel file per cuisine ("Shero Home Food - Andhra.xlsx" becomes the
   Andhra tab). DST India → Menu → **Import from sheet** → *Upload ERP item
   exports*: choose the files together. **Mrp** is the ₹ price, **Pp_price**
   (where the export has it) the cost; Category gives the sections. The ERP
   item code is the dish's id, so uploading a newer export updates the same
   dishes. Tick *Replace the whole menu* to delete every dish not in the
   files. A file that lists every brand (the full catalogue) gives only its
   own cuisine's rows. Photos load from the ERP's links at first and are
   copied to our server, 40 every five minutes. A dish without an Mrp is
   kept switched off and never sold. Prices can also be changed one by one on
   the Menu page.

6. **Zoho CRM (its own).** DST India's leads and ordering steps go to its
   own Zoho CRM, never Shero US's. In the Zoho API Console of that CRM's
   domain (api-console.zoho.in for zoho.in) add a *Server-based Applications*
   client with the Authorized Redirect URI
   `https://<domain>/dst/admin/settings/zoho/callback` (DST India → Settings
   shows it exactly). On that card paste the Client ID and Client Secret
   (stored encrypted in DST India's database), pick the domain and press
   **Connect Zoho**. Then create the bot's fields and modules in that CRM:
   `python -m scripts.setup_zoho_crm --tenant dst` lists what is missing,
   `--apply` creates it.

   **DST's own CRM (Lead Pipeline & Tracker, dst.shero.website).** Every
   ordering customer's progress also goes to DST's own CRM: an "Order Now"
   lead moved along its pipeline (welcome → requirementCapture →
   orderConfirmed → deliveryStatus; nudges add rnr1/rnr2/rnrFinal and
   lostFeedback), and on payment the contact and the order with its dishes.
   `services/dst_crm.py` writes each event to the `crm_outbox` table in the
   same transaction as the change; the `dst_crm_outbox` job posts them every
   minute, in order per customer, retrying while the CRM is down. Set
   `DST_CRM_API_URL=https://dst.shero.website` and `DST_CRM_API_TOKEN` (the
   CRM server's `ORDERING_BOT_TOKEN`). DST India → Settings shows what is
   waiting, sent and refused, with Check connection and Send now. The CRM
   side is `deploy/dst-crm-endpoint/` (its README is the handover for the
   CRM's developer).

7. **Kitchens.** DST India → Kitchens: each kitchen's address and map pin,
   delivery range in km (or a PIN code list), hours, and the WhatsApp number
   that gets its order alerts.

8. **Delivery and GST.** DST India → Settings: the delivery charge, the dish
   total above which delivery is free, the furthest distance in km, how long
   an order for now takes from ordering to the door (**Order now**, 45
   minutes by default), and the tax percent (5% GST by default; confirm it).

9. **Shiprocket riders (Mark ready).** In Shiprocket → Settings → API
   Users, add an API user for the bot (its own email; don't reuse or reset
   one another system uses) and put it on the server as
   `DST_SHIPROCKET_EMAIL` and `DST_SHIPROCKET_PASSWORD`. On each kitchen
   (DST India → Kitchens) fill in **Shiprocket pickup location**: its name
   exactly as in Shiprocket → Settings → Pickup Addresses. The account's one
   tracking webhook belongs to the website, so the bot does not use it.

10. **Switch the bot on.** DST India answers **nobody** until switched on,
   because the DST INDIA number already runs a Gallabox bot flow. On
   DST India → Bot replies:
   - add test numbers first (test mode), then choose everyone;
   - keyword mode keeps Gallabox's own flow for Party Order, Food
     Subscription and Sweets & Snacks, and starts this bot on
     **Hungry Now? Order Food** (set it as a trigger keyword);
   - in Gallabox, remove the Hungry Now branch (Swiggy / Zomato / Shero) from
     the DST flow, or customers get two replies.

## Day to day

- **Delivery times**: as on any food delivery app, customers order for
  **Now** (the default; delivered in about 45 minutes, the **Order now** time
  on Settings) or pick a later time today or in the next week, inside the
  kitchen's hours. "Now" is offered while the kitchen can deliver: open, not
  closing, and that hour not fully booked (its slot capacity). The kitchen
  is told the moment the customer pays, and the order shows as, for example,
  "Thu 19 Nov, next 45 min". Shero US is unchanged: a day's notice.
- **Orders** and **Delivery queue**: paid orders by slot, with address, map
  link and phone. *Send to kitchen → Out for delivery → Mark delivered* moves
  each on and messages the customer.
- **Messages after paying**: the payment confirmation ("Payment received!
  Your order is confirmed…"), then straight away "Your order #… is being
  prepared at <kitchen>" (`dst_order_preparing`; plain text until Meta
  approves it). The kitchen gets its alert at the same moment.
- **Mark ready** (with Shiprocket connected): when the food is prepared,
  press it on the order (Delivery queue or Orders). The bot creates the
  order on Shiprocket as a Hyperlocal (Quick) drop from the kitchen's pickup
  location to the customer's map pin, prepaid, and books the cheapest rider
  Shiprocket offers. The order then shows the courier, AWB, a **Track** link
  and **Check rider** (asks Shiprocket where the rider is). If Shiprocket
  refuses (no rider nearby, a wrong pickup name), the reason shows and
  **Try again** reuses the same Shiprocket order. Out for delivery and
  Delivered stay manual. The rider is paid from the Shiprocket wallet; the
  customer still pays the flat delivery charge set on Settings.
- **Payments**: Razorpay payment links (30 minutes, then the slot is
  released). Refunds from the order go back through Razorpay.
- **Nudges, Offers, Loyalty, Payouts**: the same pages as Shero US, with
  DST India's own rules, messages and money. Nudge messages written here are
  named `dst_nudge_…`; the 12 approved DST follow-ups (`dst_rnr_1` …
  `dst_win_back`) can be added to the list.

## Still to provide

- Razorpay keys and webhook secret (live, or test first).
- DST India's Zoho CRM: a client from its API Console, connected on the
  Settings page (above).
- India kitchens, ₹ prices, the delivery charge and free-delivery amount,
  and confirmation of 5% GST.
- Approval of the `dst_` ordering templates (submitted from the script above).
