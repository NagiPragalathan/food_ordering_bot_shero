# Retargeting nudges

Automatic WhatsApp reminders and offers that bring customers back:

| Kind | Who gets it | Default |
|---|---|---|
| `abandoned_cart` | Dishes left in the cart, no order placed | 60 minutes after the cart last changed, carts up to 23 hours old |
| `abandoned_checkout` | An order placed but never paid (link expired or payment failed) | 3 hours after, up to 3 days; their dishes go back in the cart |
| `repeat_purchase` | Paid before, nothing since | 14 days after their last order |
| `never_ordered` | Wrote in, never paid | 1 to 30 days after they first wrote |
| `targeted_offer` | Everyone the audience filters match | once each |

You can make as many campaigns as you like. For example, a 14-day reminder
and a separate 45-day win-back with a coupon, both `repeat_purchase`.

Everything comes from the bot's own database, which is fast and costs no
Zoho API calls. Zoho gets a record of every nudge (see [Zoho](#zoho)).

## The page

Sign in to the dashboard and open **Nudges** in the sidebar (for example
https://smo.shero.us/admin/nudges).

Everything below can be done on the page:

- **The master switch**, at the top.
- **Campaigns**: create from a kind or a ready-made starter, then edit every
  option. The editor has a live WhatsApp preview, placeholder buttons, and a
  JSON view of the whole campaign. Each campaign also has activate / pause,
  preview, test send, run now, duplicate and delete.
- **Activity**: every send, filterable by campaign, result and period.
- **Results**: sends, orders after a nudge, conversion and revenue for each
  campaign.
- **Customer lookup**: one number's history and opt-out, and why they would
  or would not get each campaign right now.
- **Settings**: every global option.

The page uses the same endpoints as below, with your dashboard sign-in.

---

## The master switch

**Nudges are off until you turn them on.** While they are off, nothing is
sent, not by the 5-minute schedule and not by a manual run. A new campaign
also starts **paused**, so creating one never messages anybody.

```bash
curl -X POST https://smo.shero.us/api/nudges/enable  -H "X-Ops-Key: $OPS_API_KEY"
curl -X POST https://smo.shero.us/api/nudges/disable -H "X-Ops-Key: $OPS_API_KEY"
```

Three levels of off:

| Off | Effect |
|---|---|
| `POST /api/nudges/disable` | Everything stops at once |
| `POST /api/nudges/campaigns/{id}/pause` | That one campaign stops |
| A customer replies **STOP** | That one customer gets no more nudges |

Only nudges stop. Order updates, payment links and receipts still go out.

## Signing in

Every endpoint takes either:

- the `X-Ops-Key` header (the `OPS_API_KEY` from `.env`), or
- the admin dashboard's sign-in cookie, for the future dashboard page.

## Going live safely

1. **Create** the campaign. It starts paused.
2. **Preview** it: `POST /api/nudges/campaigns/{id}/preview`. This lists who
   would get it right now, the exact message each person would read, and
   why everyone else is left out. It sends nothing.
3. **Test** it on your own phone:
   `POST /api/nudges/campaigns/{id}/test` with `{"whatsapp_number": "1732…"}`.
   Message the bot from that phone first (see
   [the 24-hour rule](#whatsapp-rules)).
4. **Dry run**: `PATCH /api/nudges/settings {"dry_run": true}`, then enable
   and activate. For a day the log shows who *would* have been sent what.
   Nothing is sent.
5. **Live**: `PATCH /api/nudges/settings {"dry_run": false}`.

To try it on real data with only your own phone: set
`{"test_numbers": ["17325550142"]}`, and only those numbers are nudged.

---

## Endpoints

| Method | Path | What it does |
|---|---|---|
| GET | `/api/nudges` | Switch, settings, campaigns, last 24 hours |
| GET | `/api/nudges/reference` | **Every option, placeholder and default**, for each kind |
| GET | `/api/nudges/options` | Coupons, kitchens, cuisines, dishes and lead sources, for pickers |
| GET / PATCH | `/api/nudges/settings` | Global settings (PATCH changes only what you send) |
| POST | `/api/nudges/enable`, `/disable` | The master switch |
| GET / POST | `/api/nudges/campaigns` | List / create |
| GET / PATCH / DELETE | `/api/nudges/campaigns/{id}` | One campaign (GET includes 30-day stats) |
| POST | `/api/nudges/campaigns/{id}/activate`, `/pause`, `/duplicate` | |
| POST | `/api/nudges/campaigns/{id}/preview` | Who would get it now. Sends nothing |
| POST | `/api/nudges/campaigns/{id}/test` | Send it to one number now |
| POST | `/api/nudges/campaigns/{id}/run` | Run it now, instead of waiting for the schedule |
| POST | `/api/nudges/preview`, `/run` | The same for all active campaigns |
| GET | `/api/nudges/sends?campaign_id=&customer_id=&status=&days=` | The send log |
| GET | `/api/nudges/stats?days=30&campaign_id=` | Sends, conversions, revenue |
| GET | `/api/nudges/customers/{number}` | One customer: history, and why they would or would not get each campaign |
| POST | `/api/nudges/customers/{number}/opt-out`, `/opt-in` | |

A mistake comes back as `422` with a sentence saying what is wrong, for
example `trigger.after_minuts: is not an option (check the spelling)`.

---

## A campaign

```json
{
  "name": "Cart reminder",
  "kind": "abandoned_cart",
  "is_active": false,
  "priority": 10,
  "promotion_code": "COMEBACK10",
  "config": {
    "trigger":  { "after_minutes": 60, "within_hours": 23, "min_items": 1, "min_value": 15 },
    "audience": { "exclude_country_codes": ["44"] },
    "message":  {
      "text": "Hi {first_name}, your {cart_items} are still waiting! Use {coupon_code} for {coupon_offer}.",
      "button": "order_link",
      "button_text": "Finish my order",
      "template": { "name": "cart_reminder_v1", "params": ["{first_name}", "{coupon_code}"], "button": "order_link" }
    },
    "schedule": { "send_window": { "start": "11:00", "end": "20:00" }, "days": ["fri", "sat", "sun"] },
    "limits":   { "max_per_customer": null, "cooldown_days": 3, "daily_cap": 100 },
    "offer":    { "auto_apply": true, "require_eligible": true },
    "zoho":     { "tag": "Nudge: Abandoned cart", "note": true }
  }
}
```

Leave out anything you do not need and the kind's default is used. A PATCH
changes only what you send: `{"config": {"limits": {"cooldown_days": 1}}}`
keeps everything else.

### trigger: when it fires

| Option | Kinds | Meaning |
|---|---|---|
| `after_minutes` | cart, checkout | How long it must be left first |
| `within_hours` | cart, checkout | The oldest cart or order still worth a reminder |
| `min_value` | cart, checkout | Smallest cart or order total |
| `min_items` | cart | Fewest dishes in the cart |
| `payment_statuses` | checkout | `["expired", "failed"]` |
| `restore_cart` | checkout | Put the unpaid order's dishes back in an empty cart (default on) |
| `days_since_last_order` | repeat | Days since their last paid order |
| `max_days_since_last_order` | repeat | Upper limit, to split a "reminder" from a "win-back" |
| `days_since_signup`, `max_days_since_signup` | never ordered | Days since they first wrote in |
| `require_details` | never ordered | Only people who gave their name and email |

### audience: narrows any campaign

`min_paid_orders`, `max_paid_orders`, `min_total_spent`, `max_total_spent`,
`last_order_within_days`, `last_order_older_than_days`,
`signed_up_within_days`, `signed_up_older_than_days`, `cuisines`,
`kitchens` (kitchen codes), `postal_codes` (prefixes: `"088"`),
`lead_stages` (the Zoho stages, e.g. `"Payment Abandoned"`), `lead_sources`,
`has_email`, `ordered_dishes` (dish names or menu codes they have bought),
`country_codes` / `exclude_country_codes` (`"1"`, `"91"`),
`whatsapp_numbers` / `exclude_whatsapp_numbers`.

### message

- `text`: up to 900 characters, with placeholders. Write `{{` and `}}` for a
  literal brace.
- `button`: `order_link` (their personal ordering page), `url` (set
  `button_url`), or `none`.
- `template`: what to send outside the 24-hour window (next section).

**Placeholders.** These work in every kind:

- `{name}`, `{first_name}`, `{shop_name}`, `{kitchen}`, `{order_link}`
- `{paid_orders}`, `{total_spent}`

These are tied to one kind:

| Placeholders | Kinds |
|---|---|
| `{cart_items}`, `{cart_total}`, `{cart_count}` | Cart |
| `{order_number}`, `{order_total}`, `{order_items}` | Checkout |
| `{last_order_items}`, `{last_order_date}`, `{days_since_last_order}` | Repeat purchase, targeted offer |

These need a coupon on the campaign:

- `{coupon_code}`, `{coupon_title}`
- `{coupon_offer}` (e.g. "20% off, up to $10")
- `{coupon_terms}`, `{coupon_ends}` (e.g. "Valid till 31 Oct.")

A placeholder that does not exist, or does not apply to the kind, is refused
when you save.

### schedule, limits, offer, zoho

| Section | Options |
|---|---|
| `schedule` | `send_window` (local hours, HH:MM, may run overnight), `days`, `starts_at` / `ends_at` |
| `limits` | `max_per_customer` (`null` = no limit), `cooldown_days`, `daily_cap`, `per_run_cap` |
| `offer` | `auto_apply`: the code is applied when they open the link. `require_eligible`: customers who cannot use the code are skipped |
| `zoho` | `tag` added to their record, `note` on their timeline |

**Coupons:** make the code on the Offers page first, then give
`promotion_code`. A **Hidden** coupon is ideal here: only the people you send
it to can use it.

---

## Global settings

`PATCH /api/nudges/settings`, defaults shown:

```json
{
  "dry_run": false,
  "test_numbers": [],
  "timezone": "America/New_York",
  "send_window": { "start": "09:00", "end": "21:00" },
  "max_per_customer_per_day": 1,
  "max_per_customer_per_week": 3,
  "min_hours_between": 20,
  "max_sends_per_run": 50,
  "attribution_days": 3,
  "block_marketing_templates_to_us": true,
  "stop_keywords": ["stop", "stop promotions", "stop offers", "unsubscribe", "opt out"],
  "start_keywords": ["start offers", "start promotions", "subscribe", "opt in"],
  "opt_out_reply": "Done - you won't get offers or reminders from us any more. …",
  "opt_in_reply": "You're back on our list - …",
  "zoho_sync": true,
  "zoho_notes": true,
  "zoho_opt_out_tag": "WhatsApp Opt-out",
  "shop_name": "Shero Home Food"
}
```

**Quiet hours** use the customer's kitchen's timezone, and `timezone` for
customers with no kitchen yet.

**Caps.** The caps apply across all campaigns together, so one customer never
gets more than one nudge a day, three a week, or two within 20 hours. If
several campaigns match one customer, the highest `priority` goes first.

---

## WhatsApp rules

- **The 24-hour window.** For 24 hours after a customer last messaged the
  bot, any message can be sent. That covers almost every abandoned cart and
  unpaid order, and costs nothing extra.
- **Outside the window**, WhatsApp accepts only an **approved template**.
  Set `message.template.name` to one approved in Gallabox. Without one,
  those customers are skipped (`outside_24h_window`) rather than sent a
  message WhatsApp would drop.
- **US numbers.** Meta currently does not deliver *marketing* templates to
  US (+1) numbers. Those sends are skipped (`us_marketing_blocked`) so you
  don't pay for messages nobody receives. If Meta lifts this, set
  `block_marketing_templates_to_us` to `false`.

In practice, cart and unpaid-order reminders reach US customers, because
they are inside the window. Reminders that come days later reach them only
if they wrote in recently. For those customers, use the Zoho tags with email
(next section).

A template with an `order_link` button must have its URL button set to
`https://smo.shero.us/order/{{1}}` when you submit it.

## Zoho

When a nudge is sent, the bot adds a **note** to the customer's Lead or
Contact: the campaign, the time, the coupon and the exact message. It also
adds the campaign's **tag**, for example `Nudge: Abandoned cart`. A customer
who replies STOP gets a note and is tagged `WhatsApp Opt-out`; START OFFERS
adds a note too. Zoho creates new tags automatically. Zoho's Free edition has
no tags: there the note (which names the tag) is the record, and the send
still counts as written to Zoho.

With those tags your team can:

- build Zoho views, such as everyone nudged this week;
- use Zoho workflows or Zoho Campaigns to follow up by email, which suits
  the US customers WhatsApp templates cannot reach.

If Zoho is down, the nudge still goes. The log shows `zoho_error`.

## Why didn't a customer get it?

`GET /api/nudges/customers/{number}` checks every campaign for that customer
and gives the first rule that stopped it:

| Reason | Meaning |
|---|---|
| `trigger_not_met` | They don't fit the trigger (no cart, not lapsed…) |
| `opted_out` | They replied STOP |
| `bot_test_mode` | The bot only answers the whitelist (Bot replies page) |
| `not_a_test_number` | `test_numbers` is set and they are not on it |
| `with_an_agent` | Handed over to a person |
| `audience:<filter>` | That audience filter leaves them out |
| `order_in_progress` | A payment link is open, or a paid order is on its way |
| `already_sent` | Already nudged about this cart, order or offer |
| `already_logged` | Already logged in dry-run mode |
| `gave_up_after_failures` | Three sends failed, so it stopped trying |
| `campaign_limit`, `campaign_cooldown` | This campaign's limits |
| `too_soon`, `daily_limit`, `weekly_limit` | The shop-wide caps |
| `quiet_hours`, `not_a_send_day` | Their local time |
| `coupon: …` | They can't use the code (first order only, used up…) |
| `outside_24h_window` | No message from them in 24 hours, and no template set |
| `us_marketing_blocked` | US number, template needed (see above) |
| `template_not_approved` | The template isn't approved in Gallabox |

## Results

`GET /api/nudges/stats?days=30` shows, for each campaign:

- sends, failures, and customers reached;
- **conversions**: paid orders within `attribution_days` of a nudge, each
  credited to the latest nudge before it;
- **revenue**, and how many of those orders used the campaign's coupon;
- how many customers have opted out.

## For engineers

| Code | What it does |
|---|---|
| `app/services/nudges/config.py` | Every option and its validation (pydantic, `extra="forbid"`) |
| `app/services/nudges/audience.py` | One finder per kind, plus the audience filters. Local database only |
| `app/services/nudges/runner.py` | The rules in order, channel choice, claiming, sending, Zoho |
| `app/services/nudges/optout.py` | STOP / START OFFERS, hooked first in `engine._handle_global_intent` |
| `app/services/nudges/reports.py` | The log and last-touch attribution |
| `app/api/routes/nudges.py` | The endpoints |
| `app/admin/routes/nudges.py`, `templates/admin/nudges.html` | The page: a shell whose script calls the endpoints. Wrapped in `{% raw %}` because of WhatsApp's `{{1}}` |
| `app/integrations/zoho/engagement.py` | Notes and tags |
| Tables | `nudge_settings` (one row; `enabled` is the switch), `nudge_campaigns` (config as JSON), `nudge_sends` (the log), and `customers.nudges_opted_out_at`. Migration `0012_nudges` |

**Scheduling.** The `nudges` job runs every 5 minutes: APScheduler on the
server, `/cron/tick` on Vercel.

**No double sends.** Each send is claimed through the unique
`nudge_sends.claim_key`, and the claim is committed before WhatsApp is
called. Two workers therefore cannot both send it. A failed send releases
its claim, so a later run retries it, up to three times.
