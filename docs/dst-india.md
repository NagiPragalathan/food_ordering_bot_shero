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
| CRM | Zoho | DST CRM (access pending); Zoho optional |

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

6. **Kitchens.** DST India → Kitchens: each kitchen's address and map pin,
   delivery range in km (or a PIN code list), hours, and the WhatsApp number
   that gets its order alerts.

7. **Delivery and GST.** DST India → Settings: the delivery charge, the dish
   total above which delivery is free, the furthest distance in km, and the
   tax percent (5% GST by default; confirm it).

8. **Switch the bot on.** DST India answers **nobody** until switched on,
   because the DST INDIA number already runs a Gallabox bot flow. On
   DST India → Bot replies:
   - add test numbers first (test mode), then choose everyone;
   - keyword mode keeps Gallabox's own flow for Party Order, Food
     Subscription and Sweets & Snacks, and starts this bot on
     **Hungry Now? Order Food** (set it as a trigger keyword);
   - in Gallabox, remove the Hungry Now branch (Swiggy / Zomato / Shero) from
     the DST flow, or customers get two replies.

## Day to day

- **Orders** and **Delivery queue**: paid orders by slot, with address, map
  link and phone. *Send to kitchen → Out for delivery → Mark delivered* moves
  each on and messages the customer.
- **Payments**: Razorpay payment links (30 minutes, then the slot is
  released). Refunds from the order go back through Razorpay.
- **Nudges, Offers, Loyalty, Payouts**: the same pages as Shero US, with
  DST India's own rules, messages and money. Nudge messages written here are
  named `dst_nudge_…`; the 12 approved DST follow-ups (`dst_rnr_1` …
  `dst_win_back`) can be added to the list.

## Still to provide

- Razorpay keys and webhook secret (live, or test first).
- The DST CRM: access or an API, to send stages to it. Until then the bot
  keeps the funnel stage on each customer and maps it to the DST CRM's
  names (`app/services/dst_crm.py`).
- India kitchens, ₹ prices, the delivery charge and free-delivery amount,
  and confirmation of 5% GST.
- Approval of the `dst_` ordering templates (submitted from the script above).
