Written for: engineers joining this codebase.

# Architecture

## Stack

| Layer | Choice | Why |
|---|---|---|
| Framework | FastAPI | All six integrations are HTTP; async-native matters when one message can touch four APIs |
| Validation | Pydantic v2 | Webhook payloads are untrusted input |
| Database | PostgreSQL + SQLAlchemy 2 (async) | JSONB for cart/context blobs, `NUMERIC` for money |
| Migrations | Alembic | |
| HTTP client | httpx + tenacity | Connection pooling and bounded retry in one place |
| Scheduling | APScheduler | Four periodic jobs; Celery + Redis would be more moving parts than the problem needs |
| Logging | structlog | JSON in production, with secret redaction |

## Request paths

There are four ways into this service.

```
1. Customer message
   WhatsApp -> Gallabox -> POST /webhooks/gallabox
     -> parse_inbound()        normalise the envelope
     -> handle_event()         dedupe, load state, dispatch
     -> step handler           one function per conversation step
     -> Gallabox send          reply

2. Payment event
   Stripe -> POST /webhooks/stripe
     -> verify signature -> locate order -> idempotent service call

3. Web ordering page
   Customer browser -> /order/{token}  (signed, expiring token)
     -> menu / address / quote / confirm -> same services as the bot

4. Outlet action
   Kitchen tablet -> POST /ops/orders/{n}/delivered  (X-Ops-Key)
     -> stage transition -> WhatsApp template -> Zoho

5. Admin dashboard
   Staff browser -> /admin/*  (signed session cookie)
     -> menu import, price edits, order queue, integration settings
```

Plus the clock: `app/workers/jobs.py` handles the 15-minute reminder, the
30-minute expiry, the feedback request and slot top-up.

## Layout

```
app/
├── core/           config, logging, exceptions, webhook auth
├── db/             models + session; models mirror the Zoho record shapes
├── schemas/        inbound webhook normalisation
├── integrations/   one package per external system - the only place that
│   ├── gallabox/     knows about anyone else's API shape
│   ├── zoho/
│   ├── meta/
│   ├── uber/
│   ├── stripe_gw/
│   └── geo/
├── services/       business logic
│   └── conversation/   the bot state machine
│       ├── engine.py       dispatcher + routing table
│       ├── handlers/       one module per stage of the flow
│       ├── prompts.py      every line the bot says
│       └── validators.py   input cleaning
├── admin/          the dashboard: auth, routes, deps
├── templates/      server-rendered admin pages (Jinja + Tailwind)
├── api/routes/     HTTP surface
└── workers/        scheduled jobs
```

The dependency direction is one-way: `api` → `services` → `integrations` →
`core`. Nothing in `integrations` imports from `services`.

## Decisions worth knowing

**The menu is ours, not Meta's.** Cuisines, categories and dishes live in this
database, imported from the client's Google Sheet and managed in the admin
dashboard. The sheet's MRP column is what a customer pays; PPP is stored as
`cost_price` for margin reporting and never reaches a customer, Stripe or
Zoho.

**Ordering happens on a web page by default.** `ORDER_MODE=web` sends a
signed link to `/order/{token}` instead of paging a 287-dish menu through
ten-row WhatsApp lists. `ORDER_MODE=chat` keeps the in-chat flow; both end at
the same Stripe link sent to WhatsApp, and both share the pricing, slot and
order services.

**Cart lines are re-priced from the menu, never trusted.** The cart is
customer-controlled state. `price_cart()` looks every line up and uses the
stored price. A test covers this specifically.

**There is one kitchen.** The multi-outlet "pick your nearest branch" flow is
gone; what remains is a delivery-area check (radius, ZIP list, or both)
configured in the dashboard.

**Admin settings override the environment.** `.env` is the floor; values saved
through the Settings page are stored encrypted (Fernet) and applied onto the
live settings singleton, so a key can be rotated without a redeploy.

**Money is `Decimal` / `NUMERIC(10,2)` everywhere.** No floats touch a
currency value. `money()` rounds half-up, not bankers'.

**CRM sync never breaks the conversation.** Everything in `crm_sync.py`
catches integration failures, logs them and returns. Zoho is the reporting
system; the local database is the system of record. This is tested by the
end-to-end test running with no Zoho credentials at all.

**Slot capacity is claimed with a conditional UPDATE**, not read-then-write:

```sql
UPDATE delivery_slots SET reserved_count = reserved_count + 1
WHERE id = :id AND reserved_count < capacity RETURNING id
```

Zero rows back means the slot filled up in the meantime, and the customer is
asked to pick another. A Python-side check would race two simultaneous payers.

**Every webhook handler is idempotent.** Stripe delivers at least once;
Gallabox retries on non-2xx. Inbound messages are deduped on the provider
message id (`inbound_messages` table), and each stage transition returns
`False` when nothing changed, so a replay sends no second notification.

**The Gallabox webhook answers 200 even when a step fails.** The engine
catches its own errors and tells the customer; a retry would re-run the half
that already succeeded. Genuine infrastructure failures still return 500.

**Outlets and slots sit behind an adapter.** `OUTLET_SOURCE` / `SLOT_SOURCE`
switch between the built-in implementation and proxying the client's own API.
The spec lists those APIs as unconfirmed, so the local implementation is the
default and switching is configuration, not a rewrite.

**All customer-facing copy is in one module.** `prompts.py` holds every line,
so wording review and translation do not touch flow logic.

## Conversation state

WhatsApp is stateless between messages, so `conversations.step` holds the
current step and `conversations.context` (JSONB) holds the working data: cart
lines, offered outlets, offered slots, the draft order id.

Routing is a table (`STEP_HANDLERS`), not a chain of conditionals, so the flow
can be read straight off it. Two things bypass the table deliberately:

- a **cart** message is handled wherever it arrives, since the customer
  chooses when to send it;
- **global keywords** (`agent`, `menu`) work anywhere *except* during
  free-text capture — otherwise someone on "Help Street" could never give
  their address.

## Failure behaviour

| Failure | What the customer sees |
|---|---|
| Zoho down | Nothing. Order proceeds; failure logged. |
| Uber quote fails | "We could not calculate the delivery charge" — we do not guess a fee. |
| Catalogue unreachable | Generic error, flow stays put. |
| Slot taken mid-flow | "That slot was just taken", slots re-shown. |
| Item unavailable | Named in the message, menu re-shown. |
| Not serviceable | Polite close, recorded in Zoho as `Not Serviceable`. |
| Stripe webhook missed | The scheduler expires the link and releases the slot anyway. |

## Known scaling limits

- **The scheduler must run on exactly one instance.** Two would double-send
  every reminder. Set `ENABLE_SCHEDULER=false` on extra replicas.
- **Menu reads hit the database on every browse step.** Fine at this size
  (287 rows); add a short-lived cache if the menu grows an order of magnitude.
- **Admin settings propagate within a minute** across instances, via the
  `settings_refresh` job - not instantly.
- **Token caches are in-process.** Each instance refreshes its own Zoho/Uber
  token. Zoho rate-limits token generation (~15 per 10 min per refresh token),
  so past ~5 instances this wants sharing.
