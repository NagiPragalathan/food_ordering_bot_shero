Written for: engineers working on this codebase.

# Testing

```bash
.venv/Scripts/python.exe -m pytest              # everything
.venv/Scripts/python.exe -m pytest -k pricing   # one area
.venv/Scripts/python.exe -m pytest -vv          # per-test names
```

287 tests, all passing, in about 7 seconds.

## What is covered

| Area | File | Notable |
|---|---|---|
| Full ordering journey | `conversation/test_happy_path.py` | Steps 1-15 driven through the real engine |
| Inbound parsing | `conversation/test_inbound_parsing.py` | Every message shape, plus malformed input |
| Validators + lead stages | `conversation/test_validators.py` | Re-ask paths, stage timestamps |
| Pricing | `services/test_pricing.py` | Catalogue pricing beats cart-claimed prices |
| Slots | `services/test_slots.py` | Generation, capacity, holds, release |
| Outlets | `services/test_outlets.py` | Distance, serviceability, cuisine filter |
| Orders | `services/test_orders.py` | Numbering, snapshots, stage transitions |
| WhatsApp payloads | `integrations/test_whatsapp_messages.py` | Length limits, all 10 templates |
| Menu import | `services/test_menu_import.py` | The real sheet layout, idempotency, id collisions |
| Kitchen serviceability | `services/test_kitchen.py` | Radius / ZIP / both modes |
| Admin settings | `services/test_settings_store.py` | Encryption at rest, live override |
| Config safety | `test_config.py` | Placeholder values never count as configured |
| Kitchen opening hours | `admin/test_kitchen_hours.py` | No hours means no slots - the form used to allow it |
| Zoho OAuth connect | `admin/test_zoho_connect.py` | Per-data-centre hosts, offline access, errors returned inside HTTP 200 |
| Ordering links | `services/test_order_link.py` | Tampered, expired and foreign-signed tokens |
| Web cart safety | `services/test_web_cart.py` | Malformed browser carts; the dev fee never applies in production |
| Shared cart | `services/test_cart.py` | Merging, exact-quantity updates, the 20 cap, corrupt lines |
| Dish images | `services/test_media.py` | Sheets formulas, Drive links, HTML rejected, no re-download |
| Photos from the sheet | `services/test_sheet_images.py` | XLSX anchors mapped to rows, downscaling, malformed workbooks |

## Tests worth knowing about

**`test_full_order_journey`** walks the entire flow — ad click through to the
payment link — against the real engine, real handlers and a real database,
asserting the conversation step and lead stage after each message. If a
refactor breaks the flow, this fails first.

It also runs with **no Zoho credentials configured**, so it proves the "CRM
sync never breaks the conversation" rule holds all the way through. The Zoho
errors in its log output are the test working as intended.

**`test_prices_from_catalogue_not_from_the_cart`** guards the security-relevant
rule that a customer-supplied cart cannot set its own prices.

**`test_declared_params_match_the_placeholders_in_the_body`** parses every
`{{n}}` out of the spec's sample bodies and checks it against the declared
parameters — so a template can never be submitted to Meta with a mismatch.

**`test_cost_price_never_reaches_the_customer`** pins the rule that the
sheet's PPP column is internal: a customer is charged MRP and nothing derived
from PPP reaches Stripe or Zoho.

**`test_items_dropped_from_the_sheet_are_hidden_not_deleted`** guards order
history — a re-import must never orphan a line on a past order.

**`test_capacity_is_exhausted_then_refused`** and
`test_switching_slots_does_not_leak_the_old_reservation` cover the slot
accounting that stops a window being oversold.

## Walking the bot by hand

In the terminal, driving the real engine with outbound messages collected
rather than delivered - nothing reaches the live Gallabox account:

```bash
python -m scripts.simulate_chat
```

Drives the **real** engine, handlers, menu and database from the terminal,
with only the outbound integrations swapped for recorders — so nothing is sent
through the live Gallabox account while testing. Uber and Stripe are stubbed
with fixed values and their output is marked `[simulated]`.

| Input | Does |
|---|---|
| any text | sends a text message |
| `/tap <id>` | taps a button or list row (prefix match, e.g. `/tap cat:`) |
| `/loc <lat> <lng>` | drops a location pin |
| `/state` | prints the conversation step and cart |
| `/reset` | forgets the test customer, so the next message starts at step 1 |

`/reset` matters more than it looks: without it the flow resumes
mid-conversation and the first thing typed is taken as the answer to whatever
question was pending.

> In Git Bash, `MSYS_NO_PATHCONV=1` stops `/reset` being rewritten as a
> Windows path. PowerShell and cmd need nothing.

## Running on SQLite

DB tests run on in-memory SQLite for speed and zero setup, and the app itself
runs on it when no Postgres is available:

```bash
DATABASE_URL=sqlite+aiosqlite:///./shero_local.db
alembic upgrade head
```

JSONB is Postgres-only, so `app/db/types.py` registers a compilation rule
mapping it to SQLite's JSON. Importing `app.db.base` registers it, so tests
and the app behave identically. Migrations use `sa.func.now()` rather than
`sa.text('now()')`, which renders `now()` on Postgres and `CURRENT_TIMESTAMP`
on SQLite — the emitted Postgres DDL is unchanged.

**Postgres is still what production runs.** Use SQLite for local flow testing,
not for judging behaviour that depends on the database:

1. **SQLite returns naive datetimes** for timezone-aware columns. Production
   code normalises them (`slots.as_utc`), which is correct either way — and is
   there because a test caught it.
2. **`UPDATE … RETURNING` works on SQLite 3.35+** but the true concurrency
   behaviour of the conditional capacity claim is a Postgres property. The
   logic is covered here; the concurrency guarantee is not.
3. **The drop-off funnel takes a different path.** The JSONB key operator is
   Postgres-only, so `_funnel_counts` falls back to counting in Python.

## Worth adding against a real Postgres in CI

- Two simultaneous holds on the last remaining slot in one window.
- A fresh `alembic upgrade head` followed by `alembic revision --autogenerate`
  producing an empty diff (catches model/migration drift).

```yaml
# sketch for GitHub Actions
services:
  postgres:
    image: postgres:16
    env: {POSTGRES_PASSWORD: shero, POSTGRES_DB: shero_test}
    options: >-
      --health-cmd pg_isready --health-interval 5s --health-retries 10
```

## Not covered

- Live calls to Gallabox, Meta, Uber, Stripe or Zoho. Every test fakes them;
  `respx` is in the dependency list for recording real HTTP exchanges once the
  credentials arrive.
- The exact Gallabox webhook envelope — see the note in
  [integrations.md](integrations.md).
- The ops API routes (the underlying stage transitions are covered).
