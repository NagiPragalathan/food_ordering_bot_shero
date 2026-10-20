# Automatic kitchen partner allocation

Automatic allocation runs in both the WhatsApp ordering flow and the ordering web page. It is enabled by the application code; there is no new switch, environment variable or database migration.

## Configure your partners

1. Sign in to the admin dashboard and open **Kitchens** (`/admin/kitchens`). Add or edit each kitchen partner.
2. Set the correct address and map pin. Distance is measured from this pin.
3. Set the delivery area: **radius**, **ZIP list**, or **both**. Both requires the address to satisfy both rules. An empty ZIP list means no ZIP restriction.
4. Fill in operating hours for the delivery days. The pin determines the kitchen timezone. A kitchen without hours has no generated delivery slots.
5. Set **Slot length** and **Orders per slot**. For example, 60 minutes and 4 orders allow four reservations in each generated hour. Save the kitchen to generate/update its slots. Existing reservations are preserved when settings change.
6. Enable **Taking orders**. Pause a partner from the Kitchens list whenever they cannot accept new orders; resume them when ready.
7. Configure the kitchen notification contact using the existing kitchen alert settings.

## What happens to a new order

- The customer supplies a delivery address or location.
- Only active kitchens covering that address qualify.
- Qualifying kitchens are ranked by distance. Equal distances keep the primary kitchen first, then the oldest kitchen. ZIP matches with unknown distance follow measured distances.
- The nearest kitchen with at least one bookable slot is selected. If it has no slots left, the next eligible kitchen is tried automatically.
- Bookable slots follow operating hours, configured lead time (`SLOT_MIN_LEAD_HOURS`, 24 by default), and the existing slot search horizon (three days after the earliest bookable time). A kitchen closed right now can accept an order for a future open window.
- The customer chooses one of that kitchen's available slots. Checkout verifies the kitchen and slot again, then atomically reserves capacity while payment is pending. Payment turns the hold into a booking; cancellation or expiry releases it through the existing lifecycle.
- The assigned kitchen is saved on the order and used by the existing pickup, kitchen alert and order reporting flows.

If every covering kitchen is full or has no bookable hours, the customer sees a temporary availability message. They are not marked as outside the delivery area. If a selected slot fills or a kitchen is paused during checkout, the customer must select a new slot; a fresh quote is required when the kitchen changes. Paid orders are not automatically reassigned.

## Try it yourself

1. Create two active kitchens with overlapping delivery areas and the same opening days. Use an address nearer kitchen A.
2. Use the Kitchens page address checker: it shows the partner currently selected. Its per-kitchen rows describe geographic coverage; the selected result also checks slot availability.
3. Start an order for that address. Kitchen A should supply the slots and appear in the order summary.
4. Pause A and start a new order/check the address again. Kitchen B should be selected if it has a bookable slot and covers the address.
5. Resume A. It is eligible again. A full individual slot disappears from the choices; A remains eligible while another slot has room. When all its bookable slots are full, B receives new allocations.
6. Pause both partners: no new order should reach payment. Existing orders remain with their assigned kitchen.
7. Open **Orders** to check the kitchen saved on each new order.

## Verification

Run `.venv/Scripts/python.exe -m pytest tests/services/test_kitchen_allocation.py -q` for allocation-specific database tests, or `.venv/Scripts/python.exe -m pytest -q` for the complete suite. New tests cover nearest selection, capacity fallback, no-hours fallback, paused/out-of-area exclusion, the final reservation, released capacity, invalid checkout and pausing after draft creation.

Tests use isolated SQLite databases and simulated integrations. Live WhatsApp/Stripe/Uber calls and simultaneous PostgreSQL transactions require a separate staging check; no live orders are placed by these tests.

## Kitchen dashboard and phone alerts

- **Admin > Kitchens > View orders** shows the partner's assigned orders. The link displays their active paid order count (paid/booked, sent to kitchen, or out for delivery).
- **Admin > Orders** also has an **All kitchen partners** filter. Choose a partner and press **Find**. Each row shows dishes and quantities, delivery notes, customer, slot, kitchen WhatsApp number, payment and fulfillment status. The existing queue shows the latest 40 matching orders.
- Edit each kitchen and enter **Kitchen WhatsApp** with its country code, then save. This is the assigned partner's recipient; the global Settings order-alert numbers are separate team notifications.
- New successful payments immediately trigger a WhatsApp alert to that assigned kitchen with the order number, customer, delivery slot, address, total and item quantities. Long carts use multiple messages so no items are omitted.
- Configure Gallabox credentials/channel and approve the existing **shero_new_order_alert** template in the connected WhatsApp account. Without approval, the existing text fallback requires an open customer-service chat window. There is no SMS integration.
- A successful provider response appears as **WhatsApp alert accepted**. This does not prove delivery to the phone or that a member has read it.
- Missing numbers and send failures remain pending, with **Retry kitchen alert** in Orders. Configure/fix the number and retry. The existing kitchen-alert worker also retries pending alerts; keep the worker/cron running. Previously queued orders keep their saved schedule and can be sent earlier using **Send to kitchen**.
- Restart the app and worker after deploying these code changes. No migration is needed. Automated tests simulate WhatsApp; no real phone messages are sent by the tests.
