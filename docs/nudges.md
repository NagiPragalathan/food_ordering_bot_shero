# Retargeting nudges

WhatsApp nudges, built to the SMO developer handoff
(`docs/Shero_SMO_Only_Developer_Handoff.pdf`): the six-month SMO follow-up
journey for people who wrote in but never ordered, plus the "separately
configured recovery rule" the handoff asks for when someone leaves an order
unfinished (section 4).

| Type | Who gets it | Default |
|---|---|---|
| `follow_up` | Wrote in, never paid: the SMO six-month journey, messages from the library | the handoff's plan (see [Follow-up journeys](#follow-up-journeys)) |
| `abandoned_cart` | Recovery: dishes left in the cart, no order placed | 60 minutes after the cart last changed, carts up to 23 hours old |
| `abandoned_checkout` | Recovery: an order placed but never paid (link expired or payment failed) | 3 hours after, up to 3 days; their dishes go back in the cart |

Those are the only types the page and the API create. The handoff keeps
post-purchase messages out of its scope and asks for one scheduler, not
separate workflows writing to the same customer, so the earlier repeat
purchase, first order and targeted offer types were removed from the page
(30 Oct 2026). Sends made by them stay in the history.

Everything comes from the bot's own database, which is fast and costs no
Zoho API calls. Zoho gets a record of every nudge (see [Zoho](#zoho)).

## The page

Sign in to the dashboard and open **Nudges** in the sidebar (for example
https://smo.shero.us/admin/nudges).

Everything below can be done on the page:

- **The master switch**, at the top.
- **Campaigns**: **New campaign** asks who it is for (the type), then every
  option is yours to set: when it starts, the follow-up stages and how often
  each reply group is messaged, the message, template, schedule and limits.
  Nothing is pre-made. **Import** brings campaigns in from Excel, CSV or JSON
  (one row per campaign, matched on its name; new ones come in paused, and
  the file is checked row by row before anything is saved), and **Export**
  writes them out in the same layout. A sample file shows the columns. The
  editor has a live WhatsApp preview, placeholder buttons, and a JSON view
  of the whole campaign. Each campaign also has activate / pause, preview,
  test send, run now, duplicate and delete.
- **Messages**: the library follow-up journeys pick from. Add, edit,
  duplicate, switch off or delete messages, or import and export them as
  Excel, CSV or JSON.
- **Activity**: every send, filterable by campaign, result and period.
- **Results**: sends, orders after a nudge, conversion and revenue for each
  campaign.
- **Customer lookup**: one number's history and opt-out, and why they would
  or would not get each campaign right now. For a follow-up journey it also
  shows their group, month, sends and next date, with move / pause / resume /
  send next now / restart / end.
- **Settings**: every global option.

Each tab has its own address, e.g. https://smo.shero.us/admin/nudges#messages.

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

### Test mode: the flows on your own phone

**Settings > Test mode > Test numbers** (`test_numbers`). While any number is
there:

* **only those numbers get nudges** - every other customer is skipped
  (`not_a_test_number`), on the live site, with real data;
* a test number always counts for the follow-up journey, as someone who
  wrote in and never ordered, whatever its past orders. An order paid
  *after* its journey started still ends the journey, as it would for a
  customer (Restart the journey to test again);
* **Customer lookup** shows the number as a test number, with **Send now**
  (**Send next now** on the follow-up) on every campaign
  (`POST /api/nudges/customers/{number}/send-now`, `runner.send_now`). It
  sends that campaign's next message straight away: no waiting days, no
  cart or payment-link wait, no 3 days after ORDER FOOD (the flow counts as
  abandoned), no quiet hours, no once-a-day or 20-hour limits. It is a real
  send: the journey moves on, Zoho is written, and the same cart or order is
  never chased twice. It works with the master switch off and the campaign
  paused. STOP, a hold, an ended journey, Dry run and WhatsApp's 24-hour rule
  still apply - message the bot from the phone first.

The scheduler keeps the real timing for test numbers too. Remove every
number to go live.

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
| GET | `/api/nudges/customers/{number}` | One customer: history, follow-up journeys, and why they would or would not get each campaign |
| POST | `/api/nudges/customers/{number}/opt-out`, `/opt-in` | |
| POST | `/api/nudges/messages/{id}/submit` | Submit the message's WhatsApp template to Meta now (again) |
| POST | `/api/nudges/templates/leftovers/dismiss` | `{"name"}` - take a template off the "to delete in Gallabox" list |
| POST | `/api/nudges/customers/{number}/send-now` | Test mode: `{"campaign_id"}` - a test number's next message from that campaign, now ([Test mode](#test-mode-the-flows-on-your-own-phone)) |
| POST | `/api/nudges/campaigns/import` | Multipart `file` (.xlsx, .csv, .json), `mode` (`upsert` / `add_only`), `commit` (false: check only). Matched on name; new campaigns are paused |
| GET | `/api/nudges/campaigns/export?format=xlsx\|csv\|json` | Every campaign as a file that imports straight back |
| GET | `/api/nudges/campaigns/sample?format=…` | An example file: a 6-month follow-up, a cart reminder, a win-back |
| GET / POST | `/api/nudges/messages` | The follow-up message library (with results and template approval) / add one |
| GET / PATCH / DELETE | `/api/nudges/messages/{id}` | One message |
| POST | `/api/nudges/messages/{id}/duplicate` | A switched-off copy |
| POST | `/api/nudges/messages/import` | Multipart `file` (.xlsx, .csv, .json), `mode` (`upsert` / `add_only`), `turn_off_missing`, `commit` (false: check only) |
| GET | `/api/nudges/messages/export?format=xlsx\|csv\|json&pool=` | The library as a file that imports straight back |
| GET | `/api/nudges/messages/sample?format=…` | A small file to start from |
| GET | `/api/nudges/journeys/summary` | Each follow-up campaign: who is in which group, month and status |
| POST | `/api/nudges/journeys/{id}` | `{"action": "move", "bucket": "later"}`, or `pause`, `resume`, `due_now`, `restart`, `end` |

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
| `not_due` | Follow-up: waiting for their next date |
| `journey_paused`, `journey_ended`, `journey_converted`, `journey_finished` | Follow-up: paused by the team, ended, ordered, or ran its length |
| `ordering_now` | Follow-up: part-way through an order (active in the last 24 hours) |
| `chatted_recently` | Follow-up: messaged the bot within the journey's wait |
| `no_message_available` | Follow-up: no active library message fits (pool, month, group, dates, dishes, coupon) |
| `no_approved_template` | Follow-up: outside the 24-hour window, and none of the fitting messages has an approved template |

## Follow-up journeys

The SMO handoff (`docs/Shero_SMO_Only_Developer_Handoff.pdf`) asks for six
months of WhatsApp messages to people who wrote in but never ordered, drawn
from a pool of 54 messages (M1-01 to M6-09), with the timing set by how they
answer. A `follow_up` campaign does exactly that, with every part editable.

**Who joins**: anyone who first wrote at least `days_since_signup` days ago
and has never paid. Their journey starts with their first follow-up.

**Which message**: the library's messages in the campaign's `pool`:

1. Month 1's messages for the first 30 days (`stage_days`), then month 2's,
   up to month 6 (`length_days` 183 / 30). A message with no month fits any.
2. At random among those that fit (or `in_order` by ID), never one of the
   customer's last 9 (`avoid_repeat`). The choice is fixed per attempt, so a
   preview shows the very message that will go.
3. An occasion message (with From/Until dates) goes first while it runs.
4. When the month's messages are all used, the nearest month's are borrowed
   (`borrow_other_months`).
5. A message is left out if its reply groups don't include theirs, its
   dishes are all off the menu, or it uses `{coupon_…}` and the campaign has
   no coupon. Outside the 24-hour window, only messages whose WhatsApp
   template is approved can go.

**When**: each reply group has its own steps, "every N days, so many times",
the last running until the journey ends:

| Group | Default | Joined by |
|---|---|---|
| No response | day 0, 3, 6, then every 10 days x3, then every 30 | everyone starts here |
| Later | every 10 days x3 from the reply, then every 30 | replying LATER, NOT NOW, MAYBE LATER, REMIND ME LATER |
| No | every 30 days from the reply, at most 6 | replying NO, NO THANKS, NOT INTERESTED |

The words and the bot's answers are in Settings, "Follow-up replies". "No"
means "not this time" (they stay opted in) unless **Treat "No" as STOP** is
on. A reply only counts from someone a follow-up has written to and who is
not part-way through an order, where "no" answers the bot's own question.
The team can also move anyone to a group from the customer lookup.

**When it ends**: a paid order (`converted`), the end of the journey
(`length_days` after the first send), or the group's last send. The
customer and their history stay; only this campaign stops ("dead lead").
STOP pauses every journey for them, as for any nudge.

**Held back**: while they are part-way through an order, or within
`pause_after_chat_hours` (12) of their last message to the bot, so nobody
gets a sales message mid-conversation. ORDER FOOD starts the ordering flow
(it must be a trigger keyword in keyword mode: the editor warns if not).

**Never twice**: each attempt has its own key (`fu:r<round>:<attempt>`), so
one attempt is sent once however many runs see it. Three failed sends count
as the attempt, so an unreachable number does not hold the journey still.
A dry run logs, and leaves the journey where it was.

**Zoho**: every follow-up's note names the message, its month, the group,
the send number and the next date. A LATER / NO reply and the end of a
journey are noted too.

### The message library

Messages tab, or `/api/nudges/messages`. Each message has an ID that never
changes (M1-01), a pool (SMO), a month, a title and angle for the team, the
text (up to 1,024 characters, with placeholders), optional reply groups,
occasion dates and dishes, and the name of its approved WhatsApp template.

**Import** an Excel, CSV or JSON file in the same columns as the export:

`ID, Pool, Month, Title, Angle, Message, Groups, Occasion, From, Until,
Dishes, WhatsApp template, Template values, Template button, Active`

Messages are matched on ID: new IDs are added, known ones updated (or left
alone with "only add new"). On an update only the columns in the file
change. The file is checked first and every row shown - new, updated, no
change, or the problem - and nothing is saved until you press Import. Rows
with a problem are skipped. "Switch off messages not in the file" turns off
the rest of the pool. The starter kit's `templates_smo.json` layout
(`id`, `month`, `title`, `body`) is read too.

`docs/nudge-messages/` has the handoff's 54 messages, word for word, ready
to import (Excel and JSON), each with its template name (`smo_m1_01`...).

**Templates** (`services/nudges/wa_templates.py`): outside the 24-hour
window WhatsApp delivers only approved templates, so every message gets its
own, submitted to Meta through Gallabox **when the message is saved** - on
the page (new or edited), or by an import (after the import, in the
background). It is named after the message (`smo_m1_01` for M1-01 in the
SMO pool), category Marketing, with the message's text; a {placeholder}
becomes {{1}}, {{2}}... and is filled in at send time.

| On the Messages tab | Means |
|---|---|
| Approved by Meta | Goes to any customer, any time - except US (+1) numbers outside 24 hours: Meta does not deliver Marketing templates there |
| Waiting for Meta | Meta is reviewing it; meanwhile only customers who wrote in the last 24 hours get it |
| Rejected by Meta | With Meta's reason. Change the text and save: a new version is submitted |
| Text changed | The text no longer matches what was submitted, so the old template is not used. Submit for approval sends the new text |
| Not submitted | Gallabox could not be reached when it was saved. Submit for approval |

Gallabox's API cannot edit or delete a template. Changed text is therefore a
new template (`_v2`, `_v3`...), and the old one is listed as "to delete in
Gallabox". Deleting a message deletes its template on Meta when
`META_SYSTEM_USER_TOKEN` and `META_WABA_ID` are set; otherwise it is listed
the same way. A listed template is never sent - no message uses it - and
drops off the list once it is gone from Gallabox (or press Done). The counts
show at the top of the Messages tab and on the Nudges header.

The 54 SMO templates were submitted on 30 Oct 2026.

## The SMO handoff, point by point

How each item of the handoff's acceptance checklist (section 12) and test
scenarios (section 13) is met. `tests/services/test_smo_handoff.py` checks
them.

| Handoff | How |
|---|---|
| Buckets and cadence (section 7) | No-response every 3 days x3, then 10 days x3, then 30; Later every 10 days x3, then 30; No every 30 days, at most 6. Each step is editable on the campaign. |
| Six months, attempt limit: Dead Lead | The journey ends after its length (183 days) or its group's last send. It shows as **Dead Lead** with the reason, and the customer and their history are kept. |
| "No" is not an opt-out | By default No means "not this time" and they stay opted in; Settings can make it STOP. An opt-out is always a suppression, never a bucket. |
| ORDER FOOD pauses nudges, an order ends them | A trigger keyword marks their journey **Engaged** (ordering flow active): no follow-up while they order. A paid order ends the journey. No order within `resume_after_days` (3): the flow is **abandoned** and follow-ups resume, never straight away (section 4). |
| Opt-out, complaint hold | STOP blocks every nudge. A **hold** (complaint, support issue, invalid contact, other) set on the Customer lookup blocks every nudge until your team lifts it. |
| Random pick, no repeats | At random from the month's eligible messages; never one of the last 9, nor the same **angle** as the last 2 (both editable). |
| Occasions (section 9) | A message with dates is an occasion: only between From and Until, never after its **Order by** cutoff, only in its **service area** (kitchens), only with one of its dishes on the menu. It replaces the month's message for that send, never both. One passed over is logged with the reason. |
| Held back: skip or defer, with the reason | Every due follow-up held back is logged (`nudge_skips`) with the reason, counted per day, and shown in Results; it is tried again on the next run. |
| Local time, with the fallback recorded | Sent in the time zone of their kitchen; without one, the Settings time zone, recorded as a fallback on the journey and in Zoho. |
| Stable template IDs, selection reason | Messages keep their IDs (M1-01 ... M6-09). Each send records why that message was picked. |
| No duplicates on retries | One claim per attempt (`nudge_sends.claim_key`); a webhook retry is ignored by message id. |
| Zoho fields (section 8) | The 20 retargeting fields on the Lead or Contact, written as the journey moves (see [Zoho](#zoho)). |
| Events and dashboard (section 11) | Results: people by retargeting bucket and month, due now, the ordering flow, Dead Leads by reason and time in journey, held back by reason, occasions passed over, per-message sends, delivery and read, failures, replies, ORDER FOOD rate, orders, revenue and average order, opt-outs, holds and Zoho failures. |

Still depends on the client's accounts (handoff section 14):

* **Approved templates.** Each message needs its WhatsApp template approved
  in Gallabox (category Marketing) to reach someone outside the 24-hour
  window. Meta currently does not deliver marketing templates to US numbers.
* **Delivery and read receipts.** Switch on Gallabox's `Message.WA.Status`
  webhook events (Settings > Webhooks) to this bot's webhook URL. Send one
  test and check the log: an unreadable receipt is logged by its keys.
* **Zoho fields.** `python -m scripts.setup_zoho_crm --apply` creates them.
  Until then the notes carry the same information.
* **Opt-out in Gallabox.** Every nudge goes through this bot, which never
  messages an opted-out customer. Gallabox's own broadcasts do not know about
  it; mark the contact opted out in Gallabox too if you send broadcasts from
  there.

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
| `app/services/nudges/reports.py` | The log and last-touch attribution, also per library message (sends, replies within 48 hours, orders) |
| `app/services/nudges/followup.py` | Follow-up journeys: who is due, the message pick, the timing per reply group, LATER / NO |
| `app/services/nudges/library.py` | The message library, and its Excel / CSV / JSON import and export |
| `app/api/routes/nudges.py` | The endpoints |
| `app/admin/routes/nudges.py`, `templates/admin/nudges.html` | The page: a shell whose script calls the endpoints. Wrapped in `{% raw %}` because of WhatsApp's `{{1}}` |
| `app/integrations/zoho/engagement.py` | Notes and tags |
| Tables | `nudge_settings` (one row; `enabled` is the switch), `nudge_campaigns` (config as JSON), `nudge_sends` (the log), and `customers.nudges_opted_out_at`. Migration `0012_nudges` |
| Follow-up tables | `nudge_messages` (the library), `nudge_journeys` (one row per customer per follow-up campaign: group, status, attempts, next date, recent messages), `nudge_sends.message_code` / `note`. Migration `0014_followup_journeys` |

**Scheduling.** The `nudges` job runs every 5 minutes: APScheduler on the
server, `/cron/tick` on Vercel.

**No double sends.** Each send is claimed through the unique
`nudge_sends.claim_key`, and the claim is committed before WhatsApp is
called. Two workers therefore cannot both send it. A failed send releases
its claim, so a later run retries it, up to three times.
