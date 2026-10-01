# Shero Home Food — WhatsApp Ordering Bot

A WhatsApp ordering flow built to the Xtracut spec: a customer clicks a Meta
ad, lands in WhatsApp, browses the menu, books a delivery slot, pays through
Stripe, and every step is tracked in Zoho CRM. Online delivery only.
 
Menu and integrations are managed from an **admin dashboard** at `/admin/`.

```
Meta Ad ──▶ WhatsApp (Gallabox) ──▶ ordering page link
                        ▲                        │
                        │            browse ─ cart ─ address ─ slot
                        │                        │
        Admin dashboard ┘     Zoho CRM ◀── Stripe ◀── Uber Direct quote
     (Google Sheet import)          payment link sent back to WhatsApp
```

## Stack

Python 3.12 · FastAPI · PostgreSQL · SQLAlchemy 2 (async) · Alembic ·
httpx · APScheduler · structlog

Every one of the six integrations is HTTP, so the service is async throughout.
Deploys as a single container.

## Quick start

```bash
py -3.12 -m venv .venv
.venv/Scripts/activate                 # source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt

cp .env.example .env                   # fill in credentials
python -m scripts.check_config         # names anything still missing

alembic upgrade head
uvicorn app.main:app --reload
```

Then open **http://localhost:8000/admin/** and sign in with the
`ADMIN_BOOTSTRAP_EMAIL` / `ADMIN_BOOTSTRAP_PASSWORD` from `.env`.
From there: **Import** the menu from your Google Sheet, then fill in
**Kitchens** (each kitchen's address, delivery radius, hours) and
**Settings** (integration credentials).

Or with Postgres included:

```bash
docker compose up --build
```

Run the tests, or walk the bot by hand:

```bash
python -m pytest                 # 287 tests, ~7s
python -m scripts.simulate_chat  # a WhatsApp conversation in the terminal
```

The simulator drives the real engine against the real menu and sends nothing
through the live Gallabox account - see [docs/testing.md](docs/testing.md).

No Postgres to hand? Set `DATABASE_URL=sqlite+aiosqlite:///./shero_local.db`
and `alembic upgrade head` works as-is. Production still runs Postgres.

## What is built

| Spec section | Status |
|---|---|
| Steps 1–19 conversation flow | Complete, including the Check Availability shortcut |
| Menu (3 cuisines, 287 dishes) | Imported from the client's sheet |
| Web ordering page | Complete — search, category filters, saved cart, address, slot, pay |
| Admin dashboard | Complete — menu, import, orders, kitchens, Uber queue, customers (delete / push to Zoho), settings |
| Delivery area + serviceability | Complete — any number of kitchens; the nearest kitchen whose area (radius, ZIP list, or both) covers the customer gets the order |
| Kitchen alert | Complete — sent to that kitchen's WhatsApp on the delivery day |
| Delivery slots + holds | Complete, generated from the kitchen's operating hours |
| Uber Direct delivery quote | Complete |
| Stripe checkout, expiry, refunds | Complete, with the short Pay Now redirect |
| Zoho: Lead through the funnel, Contact on payment, Orders + Order Items modules, dishes as Products, kitchens as Vendors | Complete; the CRM is prepared by `scripts/setup_zoho_crm.py` |
| 10 WhatsApp templates | Defined and validated in code; **await Meta approval** |
| Order fulfilment (steps 17–19) | Complete — dashboard + ops API |
| Scheduled reminder / expiry / feedback | Complete |

The service runs today with no client credentials — it starts, serves
`/health`, and reports exactly which values are missing. Fill them in one at a
time and each integration comes alive.

## Two things that need you

1. **Credentials and access** — [docs/credentials-checklist.md](docs/credentials-checklist.md)
   lists everything. Gallabox is connected and the menu is loaded; three items
   remain for a complete end-to-end test order: Stripe test keys, Uber Direct
   credentials, and the kitchen address.
2. **Two open decisions** — how the kitchen gets notified of a new paid order
   (the spec lists no template for it), and tax/currency treatment. Both in
   [docs/open-questions.md](docs/open-questions.md).
3. **Dish images** — done. All 287 photos are read out of the sheet's XLSX
   export (a CSV cannot carry them), downscaled, and served locally.

## Project layout

```
app/
├── core/           config, logging, exceptions, webhook auth
├── db/             models and session
├── schemas/        inbound webhook normalisation
├── integrations/   gallabox, zoho, meta, uber, stripe_gw, geo
├── services/       business logic + the conversation state machine
├── admin/          the dashboard (auth, routes)
├── templates/      server-rendered admin and ordering pages (_brand.html = colours)
├── static/brand/   Shero logo and favicon
├── api/routes/     webhooks, payment redirect, ops API, health
└── workers/        scheduled jobs
alembic/            migrations
docs/               see docs/README.md
scripts/            check_config, check_gallabox, simulate_chat, setup_zoho_crm
tests/              287 tests
```

## Documentation

[docs/README.md](docs/README.md) is the index. Most useful first reads:

- [architecture.md](docs/architecture.md) — decisions and layout
- [conversation-flow.md](docs/conversation-flow.md) — every spec step mapped to its handler
- [admin-dashboard.md](docs/admin-dashboard.md) — running it day to day
- [deployment.md](docs/deployment.md) — AWS, DNS, webhooks, monitoring
- [vercel-hosting.md](docs/vercel-hosting.md) — hosting on Vercel
- [aws-hosting-guide.pdf](docs/aws-hosting-guide.pdf) — step-by-step AWS hosting for the server team

## Operational notes

- **Exactly one instance may run the scheduler** (`ENABLE_SCHEDULER`), or
  every payment reminder goes out twice.
- **Two subdomains** are needed: `api.<domain>` for webhooks, `pay.<domain>`
  for the Pay Now short link (Stripe URLs are too long for a WhatsApp button).
- **Zoho failures never block an order.** The local database is the system of
  record; CRM sync is best-effort and logged.
