# Documentation

Start here.

## For the client

| Document | What it covers |
|---|---|
| [credentials-checklist.md](credentials-checklist.md) | **Everything we need from you**, grouped by system, with where to find each value |
| [admin-dashboard.md](admin-dashboard.md) | Running the bot day to day: menu, import, orders, settings |
| [zoho-setup.md](zoho-setup.md) | What the Zoho admin needs to create — fields, picklists, the Orders module |
| [whatsapp-templates.md](whatsapp-templates.md) | The 10 templates to submit for Meta approval |
| [open-questions.md](open-questions.md) | Decisions we still need, and the assumptions we made to keep moving |

## For engineers

| Document | What it covers |
|---|---|
| [architecture.md](architecture.md) | Stack, layout, and the decisions behind them |
| [conversation-flow.md](conversation-flow.md) | Every spec step mapped to its handler, plus failure paths |
| [integrations.md](integrations.md) | The six external systems and their gotchas |
| [deployment.md](deployment.md) | Local setup, AWS shape, DNS, webhooks, monitoring |
| [vercel-hosting.md](vercel-hosting.md) | Hosting on Vercel: Postgres, Blob photos, Cron jobs, data copy |
| [aws-hosting-guide.pdf](aws-hosting-guide.pdf) | Step-by-step AWS hosting for the server team (one EC2 server, SQLite) |
| [testing.md](testing.md) | What is covered, and what still needs a real Postgres |

## Quick orientation

The bot is a WhatsApp ordering flow: a customer clicks a Meta ad, chats with
the bot, browses the menu, picks a delivery slot, pays through Stripe, and
every step is tracked in Zoho CRM.

The menu (3 cuisines, 287 dishes) is imported from the client's Google Sheet
and managed in the admin dashboard at `/admin/`.

Four ways in: a customer message (Gallabox webhook), a payment event (Stripe
webhook), staff using the admin dashboard, and the ops API. Plus five
scheduled jobs.

If you are picking this up cold, read [architecture.md](architecture.md) then
[conversation-flow.md](conversation-flow.md).
