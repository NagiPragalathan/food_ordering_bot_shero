Written for: whoever deploys and operates this service.

# Deployment

Target is AWS, per the spec. The service is a single container plus Postgres.

## Local development

```bash
py -3.12 -m venv .venv                 # 3.12: prebuilt wheels for asyncpg + pydantic-core
.venv/Scripts/activate                 # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements.txt

cp .env.example .env                   # then fill it in
python -m scripts.check_config         # names anything still missing

alembic upgrade head
uvicorn app.main:app --reload
```

Then sign in at `http://localhost:8000/admin/` with the
`ADMIN_BOOTSTRAP_EMAIL` / `ADMIN_BOOTSTRAP_PASSWORD` from `.env`, import the
menu, and set the kitchen address **and its opening hours** - slots are
generated only inside those windows. See
[admin-dashboard.md](admin-dashboard.md).

Without a local Postgres, point `DATABASE_URL` at SQLite and everything above
still works:

```
DATABASE_URL=sqlite+aiosqlite:///./shero_local.db
```

That is for local flow testing only; deploy on Postgres. The differences are
listed in [testing.md](testing.md).

Or the whole stack, including Postgres:

```bash
docker compose up --build
```

## Configuration

Everything comes from the environment — see `.env.example`. Two settings are
easy to get wrong:

| Setting | Gotcha |
|---|---|
| `ZOHO_DATA_CENTER` | Must match the domain you log in at, or every call 401s |
| `ENABLE_SCHEDULER` | **Exactly one instance may have this true** |
| `ADMIN_SESSION_SECRET` | Without it the admin dashboard refuses to issue sessions |
| `SETTINGS_ENCRYPTION_KEY` | Rotating it makes saved secrets unreadable; they fall back to `.env` |
| `ADMIN_BOOTSTRAP_*` | Creates the first admin on startup. **Remove after first sign-in.** |
| `ORDER_MODE` | `web` (default) sends a storefront link; `chat` browses in WhatsApp |
| `PUBLIC_BASE_URL` | The ordering link is built from it — a wrong value sends customers nowhere |
| `DELIVERY_FEE_FALLBACK` | Testing only. Ignored when `APP_ENV=production` |

### The scheduler constraint

The scheduler runs the 15-minute reminder, the 30-minute expiry, the feedback
request and slot generation. Two instances running it would send every
customer two reminders.

Running more than one API instance:

```
instance 1:  ENABLE_SCHEDULER=true
instance 2+: ENABLE_SCHEDULER=false
```

Or run a separate one-replica service with the scheduler on and no traffic.

## DNS

Two records, both to this service:

| Host | Env var | Used for |
|---|---|---|
| `api.<domain>` | `PUBLIC_BASE_URL` | Webhooks, ops API |
| `pay.<domain>` | `PAY_REDIRECT_BASE_URL` | Pay Now short link |

`pay` must route `/<order_number>` to `/pay/<order_number>`. If you would
rather not add a rewrite, set `PAY_REDIRECT_BASE_URL=https://api.<domain>/pay`
and the same routes serve it.

Both need valid TLS — Meta and Stripe both refuse plain HTTP webhooks.

## Webhooks to register

| Provider | URL | Auth |
|---|---|---|
| Gallabox | `https://api.<domain>/webhooks/gallabox` | `X-Gallabox-Token: <GALLABOX_WEBHOOK_TOKEN>` |
| Stripe | `https://api.<domain>/webhooks/stripe` | Signature (gives you `STRIPE_WEBHOOK_SECRET`) |

Stripe events to subscribe to:

```
checkout.session.completed
checkout.session.async_payment_succeeded
checkout.session.expired
checkout.session.async_payment_failed
payment_intent.payment_failed
charge.refunded
```

## Health checks

| Endpoint | Meaning |
|---|---|
| `GET /health` | Process is up. Point the load balancer here. |
| `GET /health/readiness` | Database reachable **and** every credential present. 503 otherwise, listing missing names (never values). |

Do not gate the load balancer on readiness — a Zoho credential gap should not
take the whole service out of rotation.

## Migrations

```bash
alembic upgrade head                        # apply
alembic upgrade head --sql                  # preview the SQL, no connection needed
alembic revision --autogenerate -m "..."    # new migration
alembic downgrade -1                        # roll back one
```

Run `upgrade head` before starting new containers. The compose file does this
automatically; in ECS use a one-off task or an init container.

> When autogenerating, check the diff. `compare_server_default` is on, so a
> driver difference can produce a spurious change — the JSONB columns and the
> `now()` defaults in `0001_initial` are correct as written.

## Suggested AWS shape

| Piece | Service |
|---|---|
| API | ECS Fargate or App Runner, 2 tasks behind an ALB |
| Scheduler | Same image, 1 task, `ENABLE_SCHEDULER=true`, no ALB target |
| Database | RDS Postgres 16, Multi-AZ in production |
| Secrets | Secrets Manager or SSM Parameter Store, injected as env vars |
| Logs | CloudWatch — logs are JSON in production, so queryable |
| TLS | ACM certificate on the ALB |

The IAM user the spec asks for needs ECR push, ECS deploy, and read on the
secrets — not root.

## What to monitor

Log events worth alerting on:

| Event | Means |
|---|---|
| `zoho_ensure_record_failed` (sustained) | Zoho credentials or setup broken |
| `stripe_expiry_rejected_machine_clock_wrong` | The server's clock is wrong: sync it (NTP). Payment links still go out, but Stripe rejects every webhook signature until the clock is right |
| `uber_quote_failed` | Customers cannot check out |
| `stripe_webhook_rejected` | Wrong webhook secret |
| `kitchen_alert_failed` | A paid order may not have reached the kitchen |
| `no_slots_available` | Kitchen fully booked, or its opening hours are unset |
| `unhandled_flow_error` | A real bug — includes a stack trace |
| `setting_undecryptable` | `SETTINGS_ENCRYPTION_KEY` changed; re-enter that secret |
| `admin_login_failed` (repeated) | Someone guessing at the dashboard |
| `no_kitchen_configured` | The kitchen has not been set up in Settings |
| `order_link_unavailable` | `ADMIN_SESSION_SECRET` missing; links fall back to chat browsing |
| `delivery_fee_fallback_used` | A stand-in delivery fee was charged — must never appear in production |
| `web_payment_link_failed` | A web order was cancelled because Stripe would not issue a link |

Worth a dashboard: orders paid per hour, and lead stage counts (the drop-off
funnel the spec asks for) — both queryable straight from the database.

## Secret handling

- Secrets only ever come from the environment; none are in the repo.
- `.env` is gitignored.
- The logger redacts anything that looks like a credential, so tokens never
  reach CloudWatch.
- Readiness reports missing credentials **by name only**.
- Rotating a key means updating the secret store and restarting — no rebuild.
