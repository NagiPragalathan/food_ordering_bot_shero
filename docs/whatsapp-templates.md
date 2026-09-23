Written for: whoever submits the templates in Gallabox for Meta approval.

# WhatsApp templates

The 10 templates from section 3 of the spec. These must be created in Gallabox
and **approved by Meta before go-live** — approval usually takes minutes but
can take up to 24 hours, and a rejected template blocks that notification
entirely.

Replies inside a live conversation (welcome, menu, questions) are *not*
templates and need no approval. Only these 10 do, because they are sent
outside the 24-hour customer service window.

This page is generated from `app/integrations/gallabox/templates.py`, which is
also what the code sends — so the two cannot drift. The parameter count is
validated before any send, so a mismatch fails loudly here rather than as a
generic error from Meta.

## Before you submit

- **Category matters.** Utility templates are cheaper and are not subject to
  marketing opt-out. The two Marketing ones below are marketing by nature
  (re-engagement and a feedback request) and cannot be reclassified.
- **Language:** English (`en`). Add more locales later by extending the
  registry.
- **Dynamic URL buttons** point at the short redirect on the client's domain,
  configured in Meta as `https://pay.<domain>/{{1}}`. The bot supplies the
  order number as the button parameter. Do **not** configure the raw Stripe
  URL — Stripe checkout URLs exceed the WhatsApp button length limit.

---

## Payment templates

## 1. `payment_link`

- **Category:** Utility
- **Trigger:** Order summary confirmed (step 14-15)
- **Parameters:** `{{1}}` = customer_name, `{{2}}` = order_number, `{{3}}` = amount, `{{4}}` = slot_label
- **Button:** "Pay Now" — dynamic URL, `https://pay.<domain>/{{1}}` (parameter: order number)

**Body:**

```
Hi {{1}}, your Shero order #{{2}} comes to ${{3}} for the {{4}} delivery slot. Tap below to pay securely. The link is valid for 30 minutes.
```

## 2. `payment_reminder`

- **Category:** Utility
- **Trigger:** 15 minutes unpaid (step 15)
- **Parameters:** `{{1}}` = customer_name, `{{2}}` = order_number, `{{3}}` = slot_label
- **Button:** "Pay Now" — dynamic URL, `https://pay.<domain>/{{1}}` (parameter: order number)

**Body:**

```
Hi {{1}}, your order #{{2}} is waiting for payment. Complete it to keep your {{3}} delivery slot.
```

## 3. `payment_success`

- **Category:** Utility
- **Trigger:** Stripe payment confirmed (step 16)
- **Parameters:** `{{1}}` = order_number, `{{2}}` = amount, `{{3}}` = outlet_name, `{{4}}` = slot_label
- **Button:** none

**Body:**

```
Payment received! Your order #{{1}} of ${{2}} is confirmed from {{3}} for delivery at {{4}}.
```

## 4. `payment_failed`

- **Category:** Utility
- **Trigger:** Stripe payment failed (step 16)
- **Parameters:** `{{1}}` = customer_name, `{{2}}` = order_number
- **Button:** "Retry Payment" — dynamic URL, `https://pay.<domain>/{{1}}` (parameter: order number)

**Body:**

```
Hi {{1}}, your payment for order #{{2}} didn't go through. Please try again.
```

## 5. `payment_expired`

- **Category:** Marketing
- **Trigger:** Link expired unpaid (step 15)
- **Parameters:** `{{1}}` = customer_name
- **Button:** "Order Now" — dynamic URL, `https://pay.<domain>/{{1}}` (parameter: order number)

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

## Order and delivery templates

## 7. `order_out_for_delivery`

- **Category:** Utility
- **Trigger:** Outlet marks Out for Delivery (step 18)
- **Parameters:** `{{1}}` = order_number
- **Button:** none

**Body:**

```
Your order #{{1}} is on its way!
```

## 8. `order_delivered`

- **Category:** Utility
- **Trigger:** Outlet marks Delivered (step 18)
- **Parameters:** `{{1}}` = order_number
- **Button:** none

**Body:**

```
Your order #{{1}} has been delivered. Enjoy your meal!
```

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
How was your Shero meal today? Rate your order #{{1}}.
```

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

## After approval

Nothing to change in the code — the template names are already wired up. Just
confirm each one shows as **Approved** in Gallabox, then run one test order
end to end in Stripe test mode to see them fire in sequence:

```
payment_link -> (wait 15 min) payment_reminder -> pay -> payment_success
  -> order_out_for_delivery -> order_delivered -> (30 min) feedback_request
```
