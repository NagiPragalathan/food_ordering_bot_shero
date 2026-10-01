# Hosting on Vercel

The bot runs on Vercel as one Python function (`app/main.py`), with:

| Piece | On Vercel |
|---|---|
| Database | Hosted Postgres (Neon, from the Vercel Marketplace) |
| Dish photos | Vercel Blob |
| Timed jobs (reminders, link expiry, kitchen alerts, Uber booking) | **Off for now.** `/cron/tick` runs them when called; see [Timed jobs](#timed-jobs) |

The config is in the repo: [`vercel.json`](../vercel.json), `.python-version`
and `.vercelignore`.

> `vercel.json` has **no cron jobs**, so it deploys on the free Hobby plan
> (Hobby refuses any cron that runs more than once a day).

## 1. Create the project

1. Go to **https://vercel.com/new**, choose **Import Git Repository**, and pick
   `food_ordering_bot_shero`.
2. Vercel detects **FastAPI**. Leave the build settings as they are and click
   **Deploy**. This first deploy has no settings yet; that is expected.

## 2. Add the database and photo storage

In the project: **Storage** tab.

1. **Create Database → Neon (Postgres) → Continue**. Region:
   **Washington, D.C. (iad1)**. **Connect** it to the project, for all
   environments. Vercel adds `DATABASE_URL` and `DATABASE_URL_UNPOOLED`.
2. **Create Database → Blob → Continue**, name `shero-photos`, access
   **Public**. **Connect** it to the project. Vercel adds
   `BLOB_READ_WRITE_TOKEN`.

## 3. Add the settings

**Settings → Environment Variables**. Add every line of this computer's `.env`
**except** `DATABASE_URL`, then set or change these:

| Name | Value |
|---|---|
| `APP_ENV` | `production` |
| `PUBLIC_BASE_URL` | `https://YOUR-PROJECT.vercel.app` (or your own domain) |
| `PAY_REDIRECT_BASE_URL` | `https://YOUR-PROJECT.vercel.app/pay` |
| `CRON_SECRET` | only when switching on [Timed jobs](#timed-jobs): a long random string, e.g. from `python -c "import secrets; print(secrets.token_urlsafe(32))"` |

Keep `SETTINGS_ENCRYPTION_KEY` and `ADMIN_SESSION_SECRET` **exactly** as in
this computer's `.env`: the saved Zoho connection and settings copied in
step 4 can only be read with that key.

## 4. Copy the current data in

On this computer, in the project folder. Take the two values from
**Storage → (store) → .env.local**: `DATABASE_URL_UNPOOLED` from the Neon
store and `BLOB_READ_WRITE_TOKEN` from the Blob store.

```bash
python -m scripts.copy_to_postgres --to "DATABASE_URL_UNPOOLED value" --blob-token "BLOB_READ_WRITE_TOKEN value"
```

This creates the tables, copies the menu, kitchens, customers, orders and
settings, and uploads the 287 dish photos (with their card thumbnails) to
Blob. It refuses to write into a database that already has data; add
`--replace` to empty it first.

## 5. Deploy and check

**Deployments → the latest → Redeploy**, so it picks up the settings. Then:

```
https://YOUR-PROJECT.vercel.app/health     ->  {"status":"ok","environment":"production"}
https://YOUR-PROJECT.vercel.app/admin/     ->  the admin login
```

Then the webhooks, WhatsApp templates and Google Maps key are pointed at
the new address, as for any move of `PUBLIC_BASE_URL`.

## Updating

Push to `main`; Vercel deploys it. When a change adds a database migration
(a new file in `alembic/versions/`), apply it to the hosted database:

```bash
DATABASE_URL="DATABASE_URL_UNPOOLED value" alembic upgrade head
```

## Timed jobs

With no cron set up, these do **not** happen on Vercel: the 15-minute unpaid
reminder, the 30-minute payment-link expiry (which frees the held slot), the
delivery-day kitchen alert, the automatic Uber booking 2 hours before the
slot, and the feedback request. Ordering, payment, the confirmation and the
admin all work. Delivery slots are created as customers look for them.

To switch the jobs on later, without upgrading Vercel, add a free job at
**cron-job.org**:

| Field | Value |
|---|---|
| URL | `https://YOUR-PROJECT.vercel.app/cron/tick` |
| Schedule | every minute |
| Request header | `Authorization: Bearer <CRON_SECRET>` |

and set `CRON_SECRET` in Vercel. On the Pro plan, Vercel Cron can do the same:
add `"crons": [{ "path": "/cron/tick", "schedule": "* * * * *" }]` to
`vercel.json`.

## How it differs from a normal server

- `VERCEL=1` (set by Vercel) switches the app to serverless mode: the
  in-process scheduler never starts, no database connections are kept between
  requests, and photos that are not in Blob are written under `/tmp`.
- Settings saved in the admin reach every running instance within a minute.
- A request may run for up to 300 seconds (`maxDuration` in `vercel.json`),
  enough for a full menu import with photos.
