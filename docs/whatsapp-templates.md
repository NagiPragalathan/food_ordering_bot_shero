Written for: whoever manages the WhatsApp templates for the Shero number.

# WhatsApp templates

Thirteen templates: the 10 from section 3 of the spec, plus a welcome
opener, the web order summary and the menu link.
They must be **approved by Meta before go-live** — approval usually takes
minutes but can take up to 24 hours, and a rejected template blocks that
notification entirely.

Replies inside a live conversation (welcome, menu, questions) are *not*
templates and need no approval. Only these do, because they are sent outside
the 24-hour customer service window.

They are defined in `app/integrations/gallabox/templates.py`, which is also
what the code sends — so the wording here and the wording on the WABA cannot
drift. The parameter count is validated before any send, so a mismatch fails
loudly in our code rather than as a generic error from Meta.

---

## Names on the channel

Several templates are submitted under a different name from the spec's. The
first `payment_success` and `feedback_request` came back in error, and
`payment_link`, `payment_reminder`, `payment_failed` and `shero_welcome` were
already taken elsewhere in the Gallabox account. A name stays taken until the
template is deleted by hand in Gallabox, so the replacements are new.

The seven templates with a link button are **`_v2`** versions (submitted
1 Oct 2026): same wording, buttons and categories, but the buttons point at
the hosted address **`https://smo.shero.us`** (`/order/…`, `/pay/…`,
`/receipt/…`). A button's address is fixed when Meta approves it, and
Gallabox cannot edit a template, so a new address means new names. The bot
uses a `_v2` template only once it is approved **and** `PUBLIC_BASE_URL` is
`https://smo.shero.us`; until then it sends the plain-message fallback.

| Spec name | Name on the channel |
|---|---|
| `payment_success` | `payment_confirmed_v5` (no invoice: it goes with `order_delivered_v2`; v4 carried a Download Invoice button, v2 said Download Bill, v3 was submitted with a test address and is unused) |
| `order_delivered` | `order_delivered_v2` (with the Download Invoice button; `order_delivered` stands in until it is approved) |
| `payment_link` | `shero_payment_link_v2` |
| `payment_reminder` | `shero_payment_reminder_v2` |
| `payment_failed` | `shero_payment_failed_v2` |
| `payment_expired` | `payment_expired_v2` |
| `order_summary` | `order_summary_v2` |
| `menu_link` | `menu_link_v2` |
| `feedback_request` | `shero_feedback` |
| `shero_welcome` | `shero_welcome_message` |

To move to another address again: set `PUBLIC_BASE_URL` and
`PAY_REDIRECT_BASE_URL` to it, give those seven a new suffix in
`templates.py`, and run `python -m scripts.submit_templates --submit`.

The code keeps the spec's names for its constants (`templates.RENAMED` holds
the mapping). The sections below use the spec's names.

Gallabox lists templates 20 at a time; `template_admin.list_templates` reads
every page (100 per page), so the approval check sees all of them.

## When a template is not approved

Every template send goes through `GallaboxClient.send_template`, which asks
Gallabox whether the template is approved **before** sending (Gallabox
accepts a send for an unapproved template and then drops it silently). If it
is not approved, or Gallabox refuses it, the same message goes as an ordinary
WhatsApp message instead (`gallabox/fallback.py`):

| Template has | Sent instead |
|---|---|
| Body only | A text with the same words, parameters filled in |
| A URL button (Pay Now, Download Invoice, View Menu) | A link button with the same label and address |
| Quick replies (Great / Good / Poor, Order Now) | Reply buttons; a tap answers exactly like the template's |
| A fixed website link plus quick replies (the welcome) | The link written into the text, and the reply buttons kept |

An approved template whose link button points at a different website than
`PUBLIC_BASE_URL` counts as not approved too (log: `template_link_outdated`).
Only per-customer links (`{{1}}` in the address) are checked; the welcome's
fixed link to www.shero.us is meant to point elsewhere. A template we filed as
Utility that Meta re-filed as Marketing is not used either (log:
`template_recategorised_marketing`): Meta does not deliver marketing templates
to US numbers.
The button's address is fixed when Meta approves the template, so after the
bot moves to a new address those templates must be resubmitted; until then
the ordinary message carries the current link.

Ordinary messages are only delivered inside WhatsApp's 24-hour window.
Almost every template is sent right after the customer wrote or paid, so
this covers them; a message sent much later (a delivery update hours after
ordering) needs the approved template to arrive. The log shows
`template_not_approved_using_text` or `template_send_failed_using_text`.

## Creating them

You do not have to type these into a dashboard. One command creates every
template that does not exist yet and submits it to Meta:

```bash
python -m scripts.submit_templates --submit     # create the missing ones
python -m scripts.submit_templates --status     # what Meta has decided so far
python -m scripts.submit_templates              # just show the definitions
```

`--submit` goes through Gallabox, which forwards each template to Meta on our
behalf. It needs `GALLABOX_ACCOUNT_ID` (the 24-hex id in any Gallabox
dashboard URL) on top of the API key the bot already sends with — **not** a
Meta system-user token, which belongs to the Business Manager that owns the
WABA.

Re-running is safe: anything already on the channel is reported, not
duplicated.

> `--submit --via-meta` uses the Graph API instead, for a WABA where you do
> hold `META_SYSTEM_USER_TOKEN` and `META_WABA_ID`. Same definitions, same
> component payloads.

### Templates belong to one channel

A template lives on the WhatsApp channel it was created for. Templates on the
other Shero numbers are invisible to this one, which is why a brand-new
sending number can send nothing at all until these are created and approved.

### Fixing a rejected one

The Gallabox dev API can create and list templates but **cannot edit or delete
them** — both return 404. A template that comes back `error` or `rejected`
keeps its name, so it has to be deleted in **Gallabox → Templates** before
`--submit` can create a corrected one.

To make that rare, `template_admin.check_body()` enforces Meta's body rules
before anything is submitted. The one that has actually bitten us:

> **A body may not start or end with a variable.** Trailing punctuation does
> not count — `...for delivery at {{4}}.` is still "ending on a variable" and
> is refused. Put words after the last `{{n}}`.

## Before you submit

- **Category matters.** Utility templates are cheaper and are not subject to
  marketing opt-out. The three Marketing ones below are marketing by nature
  (re-engagement, feedback, and the welcome opener) and cannot be
  reclassified. `allow_category_change` is sent as true, so Meta may re-file a
  template rather than reject it.
- **Language:** English (`en`) — matching what `messages.template_message`
  sends. A template approved as `en` cannot be sent as `en_US`.
- **Formatting:** WhatsApp uses `*bold*`, not `**bold**`. Double asterisks
  appear literally to the customer.
- **Dynamic URL buttons** point at the short redirect on the client's domain,
  `PAY_REDIRECT_BASE_URL` + `/{{1}}`. The bot supplies the order number as the
  button parameter. Do **not** configure the raw Stripe URL — Stripe checkout
  URLs exceed the WhatsApp button length limit.

> **The URL is baked into the approved template.** Four templates carry a
> dynamic-URL button, so moving from a test tunnel to the real domain means
> deleting and re-submitting those four. Set `PAY_REDIRECT_BASE_URL` to the
> production domain before submitting for go-live.

---

## Welcome

## 11. `shero_welcome`

- **Category:** Marketing
- **Trigger:** Business-initiated opener, outside the 24-hour window
- **Parameters:** `{{1}}` = customer_name
- **Buttons:** quick reply — Order Now

**Body:**

```
👋 Hi {{1}}! Welcome to *Shero Home Food* ❤️🇺🇸

Looking for a delicious Indian vegetarian meal delivered to your home? 🍱

We make ordering simple. Tap the button below to browse our menu, build your cart and pay securely.
```

`{{1}}` is filled from `customer.greeting_name`, so a number we have never
heard from is greeted as **"Hi there!"** rather than "Hi ,". Send it with
`python -m scripts.send_welcome <number>`, which creates the lead first — see
[conversation-flow.md](conversation-flow.md) under *Opening a conversation*.

Tapping **Order Now** sends the button's own text back to us as a button
reply, which `_handle_global_intent` treats as a restart keyword and runs
onboarding from. That is why the label must stay in `prompts.RESTART_KEYWORDS`
— a test enforces it. Since the conversation is still at its first step, a lead
we contacted first is **asked for its name** before anything else.

A tapped button works even where typed text is ignored (mid-address capture,
so that somebody on "Order Street" can still give their address).

## `shero_welcome_back` and `shero_welcome_ready`

- **Category:** Utility (Marketing would not reach US numbers)
- **Trigger:** `shero_welcome_back` when a known customer writes in;
  `shero_welcome_ready` once a new customer has given name and email
- **Parameters:** `{{1}}` = customer_name
- **Buttons:** URL **Order Now** → `https://www.shero.us/` (fixed), then quick
  reply **Continue on WhatsApp**

**Bodies:**

```
Welcome back to Shero Home Food, {{1}}! 👋
```

```
Thanks, {{1}}! You are all set with Shero Home Food. 👋
```

A plain WhatsApp message cannot hold a link button and a reply button
together, which is why the greeting is a template. Tapping **Continue on
WhatsApp** sends the label back; `handle_main_menu` and, from any other step,
`_handle_global_intent` go straight on to ordering.

---

## Payment templates

## 1. `payment_link`

- **Category:** Utility
- **Trigger:** Order summary confirmed (step 14-15)
- **Parameters:** `{{1}}` = customer_name, `{{2}}` = order_number, `{{3}}` = amount, `{{4}}` = slot_label
- **Button:** "Pay Now" — dynamic URL, `<pay base>/{{1}}` (parameter: order number)

**Body:**

```
Hi {{1}}, your Shero order #{{2}} comes to ${{3}} for the {{4}} delivery slot. Tap below to pay securely. The link is valid for 30 minutes.
```

## 13. `menu_link`

- **Category:** Utility
- **Trigger:** Order Now, or a request for a new link - the web menu
- **Parameters:** none in the body
- **Buttons:** "View Menu" (dynamic URL, `<PUBLIC_BASE_URL>/order/{{1}}`,
  parameter: the customer's signed link token), then quick reply
  **Get new link**

**Body:**

```
Here is our full menu 🍛

Tap View Menu to browse all our dishes, add what you like to your cart and choose a delivery time - we will send your payment link right back here. The link is personal to you and works for a few hours.
```

Exists because a plain link message can carry only one button. Until it is
approved the bot sends a single View Menu message instead, whose footer says
to type "new link". Creating it refuses a `localhost` `PUBLIC_BASE_URL`,
because the URL is baked into the approved template.

## 12. `order_summary`

- **Category:** Utility
- **Trigger:** A web order is confirmed on the ordering page. Sent instead of
  `payment_link`, which stays the fallback while this one is not approved.
- **Parameters:** `{{1}}` = customer_name, `{{2}}` = order_number,
  `{{3}}` = items, `{{4}}` = delivery_address, `{{5}}` = slot_label,
  `{{6}}` = amount
- **Buttons:** "Pay Now" (dynamic URL, `<pay base>/{{1}}`, parameter: order
  number), then quick replies **Change menu** and **Update location**

**Body:**

```
Hi {{1}}, here is your Shero order #{{2}}.

*Items:* {{3}}
*Deliver to:* {{4}}
*Delivery:* {{5}}
*Total:* ${{6}}

Tap Pay Now to pay securely - the link is valid for 30 minutes. Need a change? Use the buttons below.
```

The layout lives in the body because a parameter may not contain line breaks:
the items arrive as one line ("2 x Drumstick Sambar, 1 x Beans Sambar",
capped, with "and N more"). **Change menu** and **Update location** release
the unpaid order (slot hold and Stripe link), put its dishes back in the cart
and send a fresh menu link; Update location's link opens the page on the
address step. A paid order is never released - the customer is told to type
*agent*.

## 2. `payment_reminder`

- **Category:** Utility
- **Trigger:** 15 minutes unpaid (step 15)
- **Parameters:** `{{1}}` = customer_name, `{{2}}` = order_number, `{{3}}` = slot_label
- **Button:** "Pay Now" — dynamic URL, `<pay base>/{{1}}` (parameter: order number)

**Body:**

```
Hi {{1}}, your order #{{2}} is waiting for payment. Complete it to keep your {{3}} delivery slot.
```

## 3. `payment_confirmed` (the spec's `payment_success`)

| | |
|---|---|
| Category | Utility |
| Trigger | Payment confirmed (step 16) |
| Body | Payment received! Your order #{{1}} of ${{2}} is confirmed from {{3}} for delivery at {{4}}. Thank you for ordering with Shero! We will send your invoice once your order is delivered. |
| Button | none |

`payment_confirmed_v5`. No invoice: the client asked for invoices to be made
and sent only after delivery, not when the order is placed, so the invoice
goes with `order_delivered_v2`. v4 carried a Download Invoice button, and a
button cannot be taken off an approved template, hence the new name. A
Download Invoice link from an older v4 message opens a page saying the
invoice comes once the order is delivered.

**Until it is approved the confirmation still arrives:** the bot checks
approval first and otherwise sends the same words as a plain WhatsApp text.
The customer paid from a link sent moments earlier, so the chat is inside
WhatsApp's 24-hour window and a plain text is delivered. Code:
`payments.send_payment_confirmation`.

## 4. `payment_failed`

- **Category:** Utility
- **Trigger:** Stripe payment failed (step 16)
- **Parameters:** `{{1}}` = customer_name, `{{2}}` = order_number
- **Button:** "Retry Payment" — dynamic URL, `<pay base>/{{1}}` (parameter: order number)

**Body:**

```
Hi {{1}}, your payment for order #{{2}} didn't go through. Please try again.
```

## 5. `payment_expired`

- **Category:** Marketing
- **Trigger:** Link expired unpaid (step 15)
- **Parameters:** `{{1}}` = customer_name
- **Button:** "Order Now" — dynamic URL, `<pay base>/{{1}}` (parameter: order number)

**Body:**

```
Hi {{1}}, your cart is still saved. Want to complete your Shero order?
```

## 6. `refund_processed`

- **Category:** Utility
- **Trigger:** Stripe refund issued
- **Parameters:** `{{1}}` = amount, `{{2}}` = order_number
- **Button:** none

**Body:**

```
Your refund of ${{1}} for order #{{2}} has been processed. It may take 5-10 business days to reflect.
```

## `shero_out_of_area`

- **Category:** Utility
- **Trigger:** the address chosen on the ordering page is outside every
  kitchen's delivery area
- **Parameters:** `{{1}}` = customer_name, `{{2}}` = delivery_address
  (street, unit, ZIP)
- **Buttons:** none

**Body:**

```
Hi {{1}}, sorry - we do not deliver to {{2}} yet. 😞

Our kitchens are not close enough to that address today, but we are growing and hope to be in your area soon.

You can try a different delivery address any time.
```

Not spam: it goes **at most once every 24 hours per customer**
(`customers.out_of_area_notified_at`, `services/out_of_area.NOTICE_EVERY`).
Every try still gets the refusal on the page itself.

## `shero_new_order_alert`

- **Category:** Utility
- **To:** the team, not the customer: the numbers under Settings > Order alerts
- **Trigger:** an order is paid (`payments.handle_payment_success`)
- **Parameters:** `{{1}}` = order_number, `{{2}}` = customer (name and phone),
  `{{3}}` = amount, `{{4}}` = delivery slot, `{{5}}` = kitchen,
  `{{6}}` = address, `{{7}}` = items (`2 x Carrot Sambar, ...`, cut at 300
  characters)
- **Buttons:** none

**Body:**

```
New order received 🛎

Order: {{1}}
Customer: {{2}}
Amount: ${{3}}
Delivery: {{4}}
Kitchen: {{5}}
Address: {{6}}
Items: {{7}}

Full details are on the admin dashboard.
```

## `shero_kitchen_order`

- **Category:** Utility
- **To:** the kitchen partner: the order's kitchen, its Kitchen WhatsApp number
  (Kitchens page)
- **Trigger:** an order is paid (`kitchen_alerts.send_alert`), or Send to
  kitchen / Retry kitchen alert on the Orders page
- **Parameters:** `{{1}}` = order_number, `{{2}}` = delivery slot,
  `{{3}}` = kitchen, `{{4}}` = items with their quantity and PPP, then the
  order's PPP total (a long cart goes as several messages, "(1/2)", never
  splitting a dish)
- **Buttons:** none

`{{4}}` reads `2 x Poondu (Garlic) Rasam - 450 ml (PPP ₹44.00 each, ₹88.00);
1 x Vazhaikkai Podimas - 450 ml (PPP ₹68.00); Total PPP: ₹156.00`. The PPP
(Triple-P) is what Shero pays the partner for the dish, as the order was
priced (`unit_ppp` on the order's lines); a dish without one says "PPP not
set". The MRP the customer paid is never shown (the client's Partner Order
Intimation requirement). The body itself is unchanged, so no new approval.

Nothing about the customer: no name, number or address, and no amount. Until
Meta approves it, the kitchen gets `shero_new_order_alert` with `{{2}}`,
`{{3}}` and `{{6}}` (customer, amount, address) as "-".

**Body:**

```
New order received 🛎

Order: {{1}}
Delivery: {{2}}
Kitchen: {{3}}
Items: {{4}}

Thank you - Shero Home Food.
```

---

## Order and delivery templates

The customer hears at three stages (services/order_updates.py), each from
the Orders page, the Delivery queue or the ops API, while Settings has
`SEND_DELIVERY_UPDATES` on (the default, at the client's request):

| Stage | Pressed | Template |
|---|---|---|
| Food Ready | **Food ready** (or **Mark ready** with Shiprocket, which also books the rider) | `order_food_ready` |
| Food Dispatched | **Out for delivery** | `order_out_for_delivery` |
| Order Delivered, with the invoice | **Mark delivered** | `order_delivered_v2` |

## 7a. `order_food_ready`

- **Category:** Utility
- **Trigger:** the food is ready: Food ready / Mark ready, once per order
- **Parameters:** `{{1}}` = order_number, `{{2}}` = the kitchen's name
- **Button:** none

**Body:**

```
Good news! Your order #{{1}} is ready at {{2}} and will be on its way to you shortly.
```

## 7. `order_out_for_delivery` (Food Dispatched)

- **Category:** Utility
- **Trigger:** Out for delivery (step 18)
- **Parameters:** `{{1}}` = order_number
- **Button:** none

**Body:**

```
Your order #{{1}} is on its way!
```

## 8. `order_delivered_v2` (Order Delivered, with the invoice)

- **Category:** Utility
- **Trigger:** Mark delivered (step 18)
- **Parameters:** `{{1}}` = order_number
- **Button:** **Download Invoice**, dynamic URL `PUBLIC_BASE_URL/receipt/{{1}}`
  (the order number signed with the app secret: it cannot be guessed or
  edited, and does not expire)

**Body:**

```
Your order #{{1}} has been delivered. Enjoy your meal! Tap below to download your invoice.
```

How the invoice goes with it:

- **The customer wrote in the last 23 hours** (inside WhatsApp's 24-hour
  window): the invoice PDF itself, as a document, with "Your order #… has
  been delivered. Enjoy your meal! Your invoice is attached." as its caption.
  One message, the invoice attached.
- **Otherwise:** this template, its button opening the invoice PDF.
- **Until Meta approves it**, outside the window: the first `order_delivered`
  ("Your order #{{1}} has been delivered. Enjoy your meal!"), without the
  invoice, so the customer still hears; the log says `invoice_waits_for_template`.

The invoice (services/receipts.py, `GET /receipt/<token>`) exists only for a
paid, delivered order, and shows the delivery date.

## 9. `order_cancelled`

- **Category:** Utility
- **Trigger:** Order cancelled by outlet
- **Parameters:** `{{1}}` = order_number
- **Buttons:** quick replies — Talk to Us

**Body:**

```
Sorry, your order #{{1}} has been cancelled. A full refund has been started.
```

## 10. `feedback_request`

- **Category:** Marketing
- **Trigger:** 30 minutes after delivery (step 19)
- **Parameters:** `{{1}}` = order_number
- **Buttons:** quick replies — Great / Good / Poor

**Body:**

```
How was your Shero meal today? Rate order #{{1}} using the buttons below.
```

> "using the buttons below" is there so the body does not end on `{{1}}`.

---

## Parameter reference

`{{1}}`, `{{2}}` … are filled positionally, in the order listed under
"Parameters" for each template above.

| Parameter | Example | Notes |
|---|---|---|
| `customer_name` | `Asha Menon` | Falls back to "there" if not captured |
| `order_number` | `SHO-260922-K4T9P` | Also the dynamic-URL button value |
| `amount` | `48.19` | Two decimal places, no currency symbol — put the symbol in the template body |
| `slot_label` | `Mon 22 Sep, 6:00 PM - 7:00 PM` | In the outlet's timezone |
| `outlet_name` | `Shero Edison` | |
| `items` | `2 x Drumstick Sambar, 1 x Beans Sambar` | One line, capped at 400 characters |
| `delivery_address` | `6360 Lawyers Hill Road, Apt 4, 21075` | Street, unit and ZIP |

## After approval

Nothing to change in the code — the template names are already wired up. Check
`--status` until everything reads `approved`, then run one test order end to
end in Stripe test mode to see them fire in sequence:

```
payment_link -> (wait 15 min) payment_reminder -> pay -> payment_success
  -> order_food_ready -> order_out_for_delivery -> order_delivered_v2 (invoice)
  -> (30 min) feedback_request
```
