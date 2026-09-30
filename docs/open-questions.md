Written for: the Xtracut project team and the Shero client contact.

# Open questions and assumptions

Points where the spec is silent or ambiguous. Each one lists what we assumed
so the build could continue, and what changes if you decide otherwise.

## Resolved

### 1. The third cuisine — **Chettinad** (23 Sep 2026)
The client's menu sheet has three tabs: Chettinad, Kerala and Andhra. All
three are imported. Cuisines are read from the imported menu, so adding a
fourth is a sheet edit, not a code change.

### 2. Outlets — **single kitchen** (23 Sep 2026)
The client confirmed they are not running multiple outlets. Spec step 11
("pick a nearby outlet") is removed; steps 6a and 10 remain as a delivery-area
check against one kitchen, configured on the admin Settings page with a
radius, a ZIP list, or both.

### 3. Pricing — **MRP is charged** (23 Sep 2026)
The sheet carries PPP and MRP at a consistent 1.53x ratio. MRP is the customer
price; PPP is stored as `cost_price` for the margin column in the admin menu
and never reaches a customer, a Stripe charge or a Zoho record.

### 4. Menu source — **this database, not the Meta catalogue** (23 Sep 2026)
The menu is imported from the client's sheet and managed in the admin
dashboard. The bot browses it cuisine -> category -> dish, because 287 items
do not fit a 10-row WhatsApp list.

*Consequence:* there is no native WhatsApp cart, so the bot keeps the cart
itself. If the Meta catalogue is populated later, an incoming native cart is
still accepted (`_adopt_native_cart`) and the native experience can be
switched back on.

## Needs a decision

### 5. Kitchen notification channel
**Spec:** step 17 says "notify the selected outlet of the new paid order and
slot", but section 3 lists no template for it.
**Problem:** a WhatsApp message to a kitchen that has not messaged us in the
last 24 hours needs an approved template; a plain text send will only arrive
if the outlet has an open session.
**Assumed:** the order is always marked `Sent to Kitchen` and exposed on
`GET /ops/orders`, and a best-effort WhatsApp text is attempted if a kitchen
number is configured.
**Options:** (a) add an 11th template, (b) email instead, (c) rely on the ops
screen. We recommend (a) — one template, approved once.

### 6. Tax
**Spec:** the summary shows "delivery charge and extra taxes/fees" from Uber,
and a separate "Total".
**Assumed:** `TAX_PERCENT=0`, i.e. dish prices are tax-inclusive and the only
extra charges are Uber's.
**If wrong:** set `TAX_PERCENT` and the tax line appears on the summary, the
Stripe page and the Zoho order.

### 7. Currency
**Assumed:** USD, from the `$` in the spec's sample templates and the Edison NJ
outlet. Set `STRIPE_CURRENCY` otherwise.

### 8. Do returning customers re-enter their address?
**Spec:** step 12 collects address, apartment, instructions and contact number,
and blocks payment until all are filled. It does not say whether a repeat
customer is asked again.
**Assumed:** asked every order, because a delivery address is per-order data
and a silently reused address is a wrong-doorstep delivery waiting to happen.
**Easy alternative:** offer the last address with a "Same as last time?"
button. Roughly a day of work; say the word.

### 9. Slot capacity
**Spec:** does not say how many orders one delivery window can absorb.
**Assumed:** configurable per outlet, default 5, with a 60-minute window.

**Decided (30 Sep 2026):** the earliest slot offered starts **24 hours after
the customer orders**, because the food is cooked to order
(`SLOT_MIN_LEAD_HOURS`, fixed in `.env`). Slots are 9 AM - 9 PM, and the Uber
courier is booked 2 hours before the slot (`UBER_DISPATCH_HOURS_BEFORE`).
Customers still see three days of slots, counted from that point.

## Assumptions we have made and can revisit

### 10. Straight-line vs road distance
Distance is great-circle (haversine) by default, which is what the "3.2 km"
display and the radius check use. A Google Maps key enables real road
distances (`DISTANCE_MODE=google_matrix`). Straight-line under-reports in
places separated by water or a highway — relevant around Edison/Jersey City.

### 11. Gallabox webhook payload shape
Our parser handles the documented Gallabox and Meta Cloud API shapes and
degrades safely on anything unrecognised rather than erroring. **One real test
message from the client's account will confirm it**, and any adjustment is
confined to `app/schemas/inbound.py`.

### 12. Gallabox agent handover mechanism
Implemented as an assign call on the conversation. If the client's workspace
uses a bot-flow handoff node instead, only
`GallaboxClient.handover_to_agent` changes.

### 13. Zoho field names
The custom field API names in `app/integrations/zoho/fields.py` are our
proposal. If the Zoho admin names them differently, change that one file.

### 14. Lead stages live in `Lead_Status`
The spec's eleven stages are mapped onto Zoho's standard `Lead_Status`
picklist rather than a new custom field, so standard Zoho funnel reports work
without configuration.

### 15. Stage timestamps stored as one JSON field
The spec requires a timestamp per stage change. Eleven separate date fields
would clutter the Lead layout, so they are written as a JSON blob in
`Stage_Timestamps`. If you want them individually reportable in Zoho, we would
need eleven datetime fields instead — tell us before the Zoho setup is done.

### 16. Payment link expiry is exactly 30 minutes
Stripe's minimum for a Checkout Session expiry is 30 minutes, which matches
the spec exactly. A shorter window is not possible with Stripe Checkout; it
would need a different payment flow.

### 17. Feedback ratings
The `feedback_request` template offers Great / Good / Poor. Stored against the
order; not currently pushed to Zoho as there is no field for it in the spec.
Say the word and we will add one.

## Out of scope as read

Called out so there is no surprise later — none of these are in the spec:

- Takeaway or pickup orders (the spec says delivery only)
- Promo codes, discounts or loyalty
- Multiple addresses saved per customer
- Pushing the menu *out* to the Meta catalogue (so the native WhatsApp cart
  works). The data is all here; it needs the Meta write API and credentials.
- Order editing after payment
- Live driver tracking for the customer (the courier is booked and the admin
  sees the Uber tracking link; the customer is not sent it)
- A customer-facing web storefront
- Multi-language support (the copy is centralised in
  `app/services/conversation/prompts.py`, so adding one is straightforward)
