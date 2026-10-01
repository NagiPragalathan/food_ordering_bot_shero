# Hosting on Vercel

The bot runs on Vercel as one Python function (`app/main.py`), with:

| Piece | On Vercel |
|---|---|
| Database | Hosted Postgres (Neon, from the Vercel Marketplace) |
| Dish photos | Vercel Blob |
| Timed jobs (reminders, link expiry, kitchen alerts, Uber booking) | Vercel Cron calling `/cron/tick` every minute and `/cron/daily` once a day |

The config is in the repo: [`vercel.json`](../vercel.json), `.python-version`
and `.vercelignore`.

> **Plan:** the every-minute cron needs the **Vercel Pro** plan. On the free
> Hobby plan Vercel only allows a daily cron and refuses to deploy the
> every-minute one; see [On the Hobby plan](#on-the-hobby-plan).

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
| `CRON_SECRET` | a long random string, for example from `python -c "import secrets; print(secrets.token_urlsafe(32))"` |

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

**Settings → Cron Jobs** lists `/cron/tick` (every minute) and `/cron/daily`.
Cron jobs run on the production deployment only.

Then the webhooks, WhatsApp templates and Google Maps key are pointed at
the new address, as for any move of `PUBLIC_BASE_URL`.

## Updating

Push to `main`; Vercel deploys it. When a change adds a database migration
(a new file in `alembic/versions/`), apply it to the hosted database:

```bash
DATABASE_URL="DATABASE_URL_UNPOOLED value" alembic upgrade head
```

## On the Hobby plan

Remove the every-minute entry from `vercel.json`, keeping the daily one:

```json
"crons": [ { "path": "/cron/daily", "schedule": "0 3 * * *" } ]
```

and call `/cron/tick` every minute from a free outside scheduler such as
**cron-job.org**: URL `https://YOUR-PROJECT.vercel.app/cron/tick`, every
minute, with the request header `Authorization: Bearer <CRON_SECRET>`.

## How it differs from a normal server

- `VERCEL=1` (set by Vercel) switches the app to serverless mode: the
  in-process scheduler never starts, no database connections are kept between
  requests, and photos that are not in Blob are written under `/tmp`.
- Settings saved in the admin reach every running instance within a minute.
- A request may run for up to 300 seconds (`maxDuration` in `vercel.json`),
  enough for a full menu import with photos.
