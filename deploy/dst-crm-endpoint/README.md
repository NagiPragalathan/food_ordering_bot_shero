Written for: the developer who maintains the DST CRM backend (dst.shero.website).

# WhatsApp ordering bot → DST CRM

The WhatsApp ordering bot (DST India's "Hungry Now? Order Food", run from
smo.shero.us/dst) sends every ordering customer's progress into this CRM:
their lead and its pipeline stage, the nudges they get, the paid order with its
dishes, the contact, each delivery step and their feedback.

It is one small, additive module. It changes none of the CRM's existing code
and needs no database migration: it writes through Prisma into the existing
tables (leads, lead_stage_history, customers, customer_addresses,
customer_verticals, orders, order_line_items, crm_activities, message_logs).

## Already live on the server (17 Nov 2026)

| Where | What |
|---|---|
| `dist/integrations/orderingBot/orderingBotService.js` | new: the event handling |
| `dist/routes/orderingBotRoutes.js` | new: the routes and the token check |
| `dist/app.js` | **two lines added**, marked `// WhatsApp ordering bot integration` |
| `.env` | `ORDERING_BOT_TOKEN=...` added (the key the bot sends) |

Backups taken before the change (code, `.env`, `app.js`, nginx and systemd
config, and a full `pg_dump` of the database):
`/opt/shero-dst/backups/ordering-bot-integration-20261117-074541/`.

**Updated 20 Nov 2026 (version `2026-11-20.1`):** one lead per customer (the
Welcome lead with no vertical is taken, see *Rules it keeps*). Only
`dist/integrations/orderingBot/orderingBotService.js` changed. Backup before
it (the old file, `dist/`, and a full `pg_dump`):
`/opt/shero-dst/backups/ordering-bot-one-lead-20261120-065350/`.

## Please add it to the source repository

The server's `dist/` was changed directly, so **the next build from your source
would drop it** (the two `app.ts` lines). To keep it, add these to the repo:

1. `src/integrations/orderingBot/orderingBotService.ts` and
   `src/routes/orderingBotRoutes.ts` (this folder; the same code as the
   compiled files above).
2. In `src/app.ts`, next to the other routes:

   ```ts
   import orderingBotRoutes from "./routes/orderingBotRoutes";
   // ...
   app.use("/api/integrations/ordering-bot", orderingBotRoutes);
   ```

3. Keep `ORDERING_BOT_TOKEN` in the server's `.env` (add it to `.env.example`
   without its value).

If a deploy does drop it, nothing is lost: the bot keeps every event in its
outbox and retries until the endpoint answers again.

## The API

All calls need the header `x-ordering-bot-token: <ORDERING_BOT_TOKEN>`
(timing-safe compare; with no token set, the routes answer 503).

| Call | What it does |
|---|---|
| `GET /api/integrations/ordering-bot/health` | Readiness: the `orderNow` vertical, the system user, the pipeline stage keys |
| `POST /api/integrations/ordering-bot/events` | One event, applied in one transaction |
| `POST /api/integrations/ordering-bot/events?dry_run=1` | The same, rolled back: nothing is saved |

Answers: `200 {success, leadId, leadCode, customerId?, orderId?, applied: [...]}`,
`400` / `422` for an event that can never be applied (the bot sets it aside),
`401` for a wrong token, `500` for anything else (the bot retries).

### Events

Every event has `event_id` (unique), `event`, `occurred_at` and `customer`
(`id` = the bot's customer id, `phone`, `name`, `email`, `address`, `pincode`).

| `event` | Extra fields | What the CRM does |
|---|---|---|
| `lead.stage` | `stage: {ours, pipeline, crm, note}` | Moves the lead's pipeline stage (`gallaboxStageKey`, `pipelineStepIndex`, history `GALLABOX_TEMPLATE`, as gallaboxWebhookService does) and CRM stage (history `CRM_STAGE`); a `note` becomes a NOTE activity |
| `nudge` | `stage`, `nudge: {kind, attempt, template, status, reply, bucket, provider_message_id, summary}` | WHATSAPP activity; a sent nudge is also an outbound `message_logs` row (linked to the stage's template); RNR / Lost Feedback stages when the bot sends them |
| `order.paid` | `stage`, `order: {...}` | Customer (found by the last 10 digits of the phone, else created), address, `orderNow` vertical; the Order (by `orderNumber`, once) with its line items, `PREPAID`, placed by the system user; ORDER and PAYMENT activities |
| `order.status` | `stage`, `order` | Order status (`CONFIRMED` / `DELIVERED` / `CANCELLED`), pipeline stage, ORDER activity, PAYMENT activity for a refund |
| `order.feedback` | `order` (with `feedback`) | NOTE activity |

`order` carries the whole order every time (number, items with name, sku,
quantity, rate, amount; subtotal, delivery charge, discount, tax, total,
delivery date and slot, address, pincode, coordinates, kitchen, Razorpay
payment id, instructions, refund), so any order event can create the order if
an earlier one never arrived.

### Rules it keeps

- **One lead per customer** (DST's lead design, 19 Nov 2026, "update the SAME
  lead"). Each ordering customer has one lead in the `orderNow` ("Order Now")
  vertical, found again by `externalSource = ORDERING_BOT` and `externalId` =
  the bot's customer id. The first time, an unclaimed lead with the same
  number is taken instead of making a new one: an Order Now lead, else the
  **Welcome lead with no vertical yet** that `gallaboxWebhookService` made
  when they first messaged, whose vertical becomes Order Now (as a button
  reply sets Party / Subscription / Sweets). A lead in another vertical (a
  party order, a subscription) is never touched. *(Version 2026-11-20.1.)*
- **Nothing typed by the team is overwritten.** Name, email, location,
  pincode are only filled in when empty (a name that is just a phone number is
  replaced). A lead marked `WON` stays `WON`.
- **Replays are harmless.** An order is made once (unique `orderNumber`); an
  activity carries `Ref: <event_id>/<TYPE>` and is not added twice; a message
  log is not added twice.
- **No side effects.** Orders are inserted directly, so no kitchen
  auto-assignment job is created and no WhatsApp message is sent by the CRM.
- **Created once, on the first event:** the `orderNow` vertical (sort order 5)
  and the system user `whatsapp-ordering-bot@system.invalid`, inactive, with a
  random password: it cannot sign in (authService refuses inactive users).

## The pipeline as the bot fills it

| Bot step | Pipeline stage | CRM stage |
|---|---|---|
| First message | `welcome` (+ a note with the original source: Meta ad with ad / campaign, or the DST number) | NEW |
| Name / email given (the customer's own answer) | `requirementCapture` | CONTACTED |
| Cart, address, time, payment link, abandoned or failed payment | `requirementCapture` | IN_DISCUSSION |
| Paid (verified by Razorpay; a test payment says "TEST - simulated payment, nothing charged") | `orderConfirmed` | WON |
| Sent to kitchen | `orderConfirmed` | |
| Mark ready: Shiprocket rider booked (the dispatch) | `deliveryStatus` (+ a note: courier, AWB, fee, tracking link) | |
| Rider booking cancelled | (a note) | |
| Out for delivery, delivered | `deliveryStatus` | |
| No-response follow-up 1 / 2 / 3+ | `rnr1` / `rnr2` / `rnrFinal` | |
| Template `dst_rnr_1`, `dst_rnr_2`, `dst_rnr_final`, `dst_win_back` | that stage | |
| Nudge answered No or STOP | `lostFeedback` | LOST (not if WON) |

## Undo

To switch it off: remove the two marked lines from `dist/app.js` and restart
`dst-backend` (or restore `app.js.backup` from the backup folder above). The
records it created are ordinary CRM records (source "WhatsApp Ordering Bot",
vertical "Order Now").
