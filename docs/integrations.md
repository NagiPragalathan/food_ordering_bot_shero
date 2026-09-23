Written for: engineers wiring up or debugging the external systems.

# Integrations

Six external systems. Each lives in its own package under
`app/integrations/`, and nothing outside that package knows anyone else's API
shape — so a change upstream touches one file.

Shared plumbing is in `integrations/base.py`: pooled httpx client, timeouts,
bounded retry (3 attempts, exponential backoff, only on 429/5xx/network), and
a typed `IntegrationError` on any non-2xx.

---

## Gallabox (WhatsApp)

**Auth:** static `apiKey` / `apiSecret` headers.
**Files:** `client.py` (HTTP), `messages.py` (payload builders),
`templates.py` (the 10 approved templates).

Message builders are pure functions, so the exact bytes we send are unit
tested. They enforce WhatsApp's silent limits by truncation:

| Limit | Value |
|---|---|
| Reply button title | 20 chars, max 3 buttons |
| List row title | 24 chars, max 10 rows total |
| List row description | 72 chars |
| Products per message | 30, across max 10 sections |

> **Confirm on day one:** the exact JSON envelope Gallabox posts to our
> webhook. `app/schemas/inbound.py` handles the documented Gallabox and Meta
> Cloud API shapes and degrades to "unrecognised" rather than throwing, and
> logs the raw body. One real test message settles it. Any adjustment is
> confined to that one file.

**Inbound webhook:** `POST /webhooks/gallabox`, authenticated with a shared
token we generate (`GALLABOX_WEBHOOK_TOKEN`) sent as `X-Gallabox-Token` or
`Authorization`.

**Agent handover** is implemented as a conversation assign call. If the
client's workspace uses a bot-flow handoff node, change
`GallaboxClient.handover_to_agent` only.

## Meta Catalogue

**Auth:** Bearer system user token with `catalog_management`.
**Endpoint:** `GET /{catalog_id}/products`, cursor-paginated (capped at 20
pages as a runaway guard).

Cached in-process for `META_CATALOG_CACHE_TTL_SECONDS` (default 15 min) — the
menu changes rarely but is read on every conversation.

Two field conventions to confirm with the client:

| Catalogue field | Used as |
|---|---|
| `product_type` (or `custom_label_0`) | Cuisine |
| `custom_label_1` | Outlet code, if the dish is outlet-specific |

Blank means "available everywhere" in both cases, so an unlabelled catalogue —
the likely starting state — does not hide the menu or block orders.

`parse_price` handles the several shapes Graph returns: `"$12.00"`,
`"1,234.50 USD"`, `"1234,50"`, or an integer in minor units.

## Zoho CRM

**Auth:** OAuth refresh-token grant → cached access token (1 hour).
**Files:** `oauth.py`, `client.py` (transport), `crm.py` (operations),
`fields.py` (every field API name, in one place).

Three things that bite:

1. **Data centre isolation.** A `.in` org rejects `.com` credentials. Set
   `ZOHO_DATA_CENTER` to match the login domain.
2. **Errors inside a 200.** Zoho reports per-record failures in the body of a
   successful HTTP response. `_first_record_id` inspects `status: error` and
   raises, so a rejected write is not silently ignored.
3. **Leads require `Last_Name` and `Company`.** `split_name` handles
   single-word and empty names; `Company` is a constant.

Leads are written with **upsert on `Phone`**, so a webhook replay or two
simultaneous messages cannot create a duplicate Lead.

See [zoho-setup.md](zoho-setup.md) for what the admin must configure.

## Uber Direct

**Auth:** OAuth client-credentials → cached token.
**Endpoint:** `POST /v1/customers/{customer_id}/delivery_quotes`.

Amounts come back in **minor units** (cents) and are converted to `Decimal`
immediately. `fee` becomes the delivery charge, `tax` becomes the "extra
taxes/fees" line — the two the spec shows separately on the summary.

Addresses are sent as JSON strings (`build_address`), with coordinates
alongside, because a ZIP-only dropoff geocodes poorly on Uber's side.

**If the quote fails, the order is blocked**, not estimated. Charging a
guessed delivery fee is worse than asking the customer to retry.

## Stripe

**Package is `stripe_gw`**, not `stripe`, so it cannot shadow the SDK.

Checkout Sessions are created with an explicit `expires_at`, so Stripe itself
enforces the 30-minute link lifetime and emits `checkout.session.expired`.
Stripe's minimum expiry is exactly 30 minutes, which matches the spec;
`clamp_expiry_minutes` keeps any misconfiguration inside the accepted window.

Line items are itemised (each dish, delivery, taxes and fees) so the Stripe
page shows the same breakdown as the WhatsApp summary.

**Webhook events handled** (`POST /webhooks/stripe`, HMAC-verified):

| Event | Action |
|---|---|
| `checkout.session.completed` | Book slot, convert Lead, file Order, alert kitchen |
| `checkout.session.expired` | Release slot, `payment_expired` template |
| `payment_intent.payment_failed` | `payment_failed` template with retry |
| `charge.refunded` | Record a refund issued from the Stripe dashboard |

A completed session is only treated as paid when `payment_status` is `paid` or
`no_payment_required` — a delayed payment method can complete unpaid.

The order is located from `metadata.order_id`, falling back to the order
number, session id, then payment intent id.

## Geocoding and distance

Only needed when a customer types a ZIP instead of sharing a pin.

| Backend | When |
|---|---|
| Google Geocoding | `GOOGLE_MAPS_API_KEY` is set |
| OpenStreetMap Nominatim | otherwise — free, ~1 req/sec, no SLA |

Results are cached for the process lifetime (a postal code's centroid does not
move). A geocoder failure returns `None` rather than raising, which becomes
the spec's "re-ask if location can't be read" path.

Distance is great-circle (haversine) by default. Straight-line under-reports
where water or a highway intervenes — relevant around Edison/Jersey City — so
`DISTANCE_MODE=google_matrix` is available with a key.

## Client backend (optional)

`OUTLET_SOURCE` / `SLOT_SOURCE` = `remote` proxies to the client's own APIs
instead of using ours. Expected shapes:

```
GET /outlets/nearby?latitude=&longitude=&postal_code=&cuisine=
  -> {"outlets": [{"id","name","code","distance_km","is_serviceable","address"}]}

GET /outlets/slots?outlet_id=&days=
  -> {"slots": [{"id","label","starts_at","ends_at","remaining"}]}
```

Both to be confirmed with the client — the local implementation is the default
precisely because these APIs are unconfirmed.
