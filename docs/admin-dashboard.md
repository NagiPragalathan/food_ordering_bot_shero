Written for: the Shero team who will run the bot day to day.

# Admin dashboard

`https://api.<yourdomain>/admin/` — or `http://localhost:8000/admin/` when
running locally.

Five screens in the sidebar: **Dashboard, Menu, Orders, Chat tester,
Settings**.

The **Import** page is hidden from the sidebar but still works — go straight
to `/admin/import`. Only the nav entry was removed, so putting it back is a
one-line change in `app/templates/admin/base.html`.

---

## Signing in

The first account is created on startup from `ADMIN_BOOTSTRAP_EMAIL` and
`ADMIN_BOOTSTRAP_PASSWORD` in `.env`. **Remove both once you have signed in**
— they are only there to get you through the door the first time.

Passwords are stored as Argon2 hashes and can never be read back. Sessions are
signed cookies that expire after `ADMIN_SESSION_HOURS` (default 12).

---

## Dashboard

Paid orders, revenue, customers and live menu size, plus two things worth
looking at daily:

- **Drop-off funnel** — how many customers reached each stage. This is the
  report the spec asks for: if most people reach "Cart Created" and few reach
  "Payment Link Sent", the problem is between those two steps.
- **Needs attention** — paid orders not yet delivered.

A banner at the top lists any credentials still missing.

---

## Menu

The whole menu, grouped by cuisine and category exactly as your sheet is.

You can run the menu entirely from this page - the sheet import is for loading
it in bulk, not the only way in.

### Adding things

| Button | What it does |
|---|---|
| **Add a dish** | Name, category, price, cost, description, pack size, serves and a photo. It is on sale the moment you save. |
| **New category** | A section inside the cuisine you are looking at, e.g. "Festival Specials" |
| **New cuisine** | A whole new tab. Add a category in it before adding dishes - a dish has to live in a category. |

A photo can be **uploaded** or given as a **link**. Either way it is downscaled
and stored on this server rather than hot-linked, exactly as the importer does,
so a customer's page never depends on somebody else's host staying up. Leave
both empty and the dish shows a drawn placeholder.

> A dish added here gets the **same id the importer would give it** (cuisine +
> category + name). So if the same dish later appears in your sheet, the import
> updates this row instead of creating a duplicate.

### Editing

Each row has a quick edit for the two things that change most - **price** and
**Available**. Saving there touches nothing else, so it cannot blank a
description by accident.

**Edit full details** opens the rest: name, cost, description, pack size,
serves and a replacement photo. Leave the photo empty to keep the current one.

> Renaming a dish **does not** change its id. Carts people are holding right
> now, paid orders and Stripe records all point at that id, so it stays put.

### Removing

| Action | Effect |
|---|---|
| **Available** unticked | Hidden from customers, kept on past orders. Reversible. |
| **Delete** on a dish | Gone for good |
| **Delete the ... category** | The category and every dish in it |
| **Delete cuisine** | The cuisine, its categories and all of their dishes |

Each asks you to confirm first, and says how many dishes will go.

Deleting is safe for your records: an order stores its own copy of what was
bought, so a delivered order still shows the right dishes and prices after the
dish is deleted. Someone holding that dish in an open cart is told it is
unavailable at checkout - the same path an out-of-stock dish takes.

**Hiding is usually the better choice** for something seasonal. Delete is for
things that were wrong to begin with.

### Also on this page

- Each dish shows its **margin** - price minus cost, from the sheet's PPP
  column.
- **Hide from menu** takes a whole cuisine out of WhatsApp without deleting
  anything.
- **Search** matches dish names within the cuisine you are viewing.
- **Sync to Zoho** (top right) sends every dish to Zoho's Products module
  and every kitchen to its Vendors module, creating or updating each. Adding
  or editing a dish here updates its Product on its own; run the sync after
  a sheet import, or after editing kitchens in Settings. It is greyed out
  until Zoho is connected. Details in [zoho-setup.md](zoho-setup.md).

> Prices here are what customers pay (your MRP column). The PPP column is
> stored as cost and is shown only as a margin figure - it never reaches a
> customer, a Stripe charge or a Zoho record.

---

## Import

Two ways to load the menu.

### From Google Sheets

Paste the sheet URL. Every tab is imported as one cuisine.

The sheet must be shared as **Anyone with the link can view**.

Expected layout, matching your existing sheet:

```
      Chettinad Cuisine - USA Menu

  1   Sambar      Description  IMAGE  PPP   MRP
  1   Drumstick   16 oz pack…         10.78 16.53
  2   Beans       16 oz pack…          7.36 11.28

  2   Rasam       Description  IMAGE  PPP   MRP
  1   Tomato      …                    6.20  9.50
```

The rules the importer follows:

| Rule | Why |
|---|---|
| A row is a **category** when its 4th column reads "Description" | That is how your sheet marks sections |
| The cuisine comes from the title row ("Chettinad Cuisine - USA Menu") | So each tab names itself |
| **MRP** is charged; **PPP** is stored as cost | Confirmed with you |
| Pack size and serving count are pulled out of the description | Shown under each dish in WhatsApp |

### From a CSV

One file, one cuisine. Useful if a tab is edited offline. If the file has no
title row, type the cuisine name in the box.

### Dish photos

Photos come from the sheet itself. **The IMAGE column looks empty in a CSV
export because the pictures are anchored over the cells rather than stored in
them** — so the importer also pulls the XLSX export, which does carry them,
and matches each picture to its dish by row.

All 287 dishes currently have their photo.

Links in the IMAGE column are handled too, if you ever use them instead:

| In the cell | What happens |
|---|---|
| A picture pasted into the sheet | Read from the XLSX export, matched by row |
| `https://…/dosa.jpg` | Downloaded as-is |
| `=IMAGE("https://…")` | The formula is unwrapped first |
| A Google Drive share link | Rewritten to the direct-download form |
| Nothing at all | The dish shows a drawn placeholder |

Photos are **downscaled to 800px JPEG** on the way in — the sheet's originals
are around 850 KB each, which 287 of would make the menu unusable on a phone.
The whole set lands at roughly 25 MB, about 87 KB per dish.

Photos are **stored once and served from this server** (`data/media/`), never
hot-linked — a customer's page must not depend on Drive staying up or staying
public. Re-importing overwrites in place rather than piling up duplicates.

> The XLSX export is large (a few hundred MB for this menu), so a sheet import
> takes a minute or two. The import still succeeds if that download fails; you
> simply get the menu without new photos.

The import summary reports how many were stored and how many could not be
fetched; a broken link never fails the import.

> If a photo does not appear, the usual cause is a Drive file that is not
> shared as "Anyone with the link can view" — the server then receives Google's
> sign-in page instead of an image, and rejects it.

### Re-importing is safe

Rows are matched on a generated id (cuisine + category + dish), so importing
an edited sheet **updates prices in place** rather than creating duplicates.

Items that have disappeared from the sheet are **hidden, not deleted** — an
order placed last week must still show what was in it. Untick "Hide items that
are no longer in the sheet" if you are importing a partial file.

---

## Orders

The live queue. Filter by stage or search an order number.

Each paid order has one button that moves it forward:

```
Paid & Slot Booked  ->  Send to kitchen
Sent to Kitchen     ->  Out for delivery   (sends order_out_for_delivery)
Out for Delivery    ->  Mark delivered     (sends order_delivered)
```

Advancing an order **sends the matching approved WhatsApp template to the
customer** and updates the stage in Zoho. A double-tap is safe: the second
press does nothing rather than sending a second message.

Thirty minutes after delivery, the feedback request goes out automatically.

---

## Customers

Everyone who has messaged the bot, newest first, with their funnel stage,
where the chat is, orders and saved addresses, and whether they are in Zoho.
Search by name, number or email.

| Button | What it does |
|---|---|
| **Push to Zoho** | Finds or creates the customer's Zoho record (their Lead, or their Contact once they have paid), then sends their name, email, default address, kitchen, distance, cuisine, Bot Stage and stage history. A paid customer still held as a Lead is converted to a Contact, and any of their orders filed under the Lead are moved to the Contact. Any **paid** order Zoho does not have yet is filed in Orders under that record, linked to its kitchen's Vendor, with one Order Item per dish linked to the dish's Product; an order already there gets those links and items brought up to date. A record deleted in Zoho is recreated. Safe to press again - nothing is filed twice. If Zoho refuses something, the error is shown at the top of the page. |
| **Push all to Zoho** | The same for every customer on the page not marked *In Zoho*. |
| **Delete** | Removes the customer **here only**: chat state, saved addresses, orders and their slot holds (held or booked delivery windows are freed). Their next WhatsApp message starts from the welcome, as a new customer. Zoho is not touched - delete the Lead or Contact there yourself if you want it gone. Meant for clearing test data; it asks for confirmation and cannot be undone. |

The **Zoho** column is checked against Zoho on every load, never taken from
the saved id alone:

| Badge | Meaning |
|---|---|
| Zoho Lead / Zoho Contact | The record exists in Zoho and every paid order is there |
| N order(s) to push | The record exists, but paid orders are missing in Zoho |
| Deleted in Zoho | The bot had a link, Zoho no longer has the record; the link is dropped so the next push recreates it |
| Not in Zoho | Never pushed |
| Zoho Lead (could not check) | Zoho did not answer; nothing is changed |
| Zoho not connected | No Zoho connection; nothing can be checked or pushed |

Zoho record ids belong to one org. Connecting the bot to a **different** org
(Settings → Connect Zoho) drops every saved link and says so, because those
ids mean nothing there; Push to Zoho recreates the records. Code: `app/services/customer_admin.py`, `crm_sync.push_customer`.

---

## Settings

Where you connect everything. Values saved here **override `.env` and take
effect immediately** — no redeploy to rotate a key.

| Group | What it connects |
|---|---|
| WhatsApp (Gallabox) | API key, secret, channel ID, webhook token |
| Zoho CRM | Domain dropdown and **Connect Zoho** |
| Stripe | Secret key, publishable key, webhook secret, currency |
| Uber Direct | Customer ID, client ID, client secret |
| Meta Catalogue | Catalogue ID, system user token |
| Maps | Google Maps key (optional) |
| Business rules | Tax, payment timings, public URLs |

### Test connection

Each group has a **Test connection** button that makes one real, read-only
call and reports back in plain language — "Connected to Shero USA in TEST
mode, charges enabled", or "The refresh token is invalid or has been revoked".

Nothing a Test button does sends a message, charges a card or writes a record.
It is safe to press repeatedly.

### About secrets

Secret fields are **encrypted at rest** with `SETTINGS_ENCRYPTION_KEY` and are
never rendered back to the browser — an existing secret shows as "set" and an
empty box means "leave it as it is". To change one, type the new value.

If `SETTINGS_ENCRYPTION_KEY` is not set, secret fields are disabled rather
than stored in the clear.

> Rotating `SETTINGS_ENCRYPTION_KEY` makes previously saved secrets
> unreadable. They fall back to whatever is in `.env`, and the log records
> `setting_undecryptable`. Re-enter them here afterwards.

### Zoho CRM

Zoho is connected by OAuth rather than by pasting a refresh token. The card
is a **domain** dropdown (zoho.com, zoho.in, ...) and **Connect Zoho**. The
Client ID and Secret come from `.env`, and the card says so if they are
missing. The consent screen opens on the chosen domain; the data centre
Zoho reports in its reply wins over the choice. The client needs **Multi-DC** turned on, and the
redirect URI to register is in [zoho-setup.md](zoho-setup.md).

Once connected the card shows **connected**, with **Test connection** and
**Disconnect**. Disconnect deletes the stored refresh token and leaves the
Client ID and Secret in place, so reconnecting is one press.

### Kitchen and delivery area

Below the integrations: the kitchen address (sent to Uber as the pickup), its
coordinates, and who you deliver to.

Three ways to define the delivery area:

| Mode | Rule |
|---|---|
| **Within a radius** | Straight-line distance from the kitchen |
| **Only these ZIP codes** | The customer's ZIP is on your list |
| **Radius AND ZIP list** | Both must pass |

A ZIP list is usually the honest choice for a small operation: it says exactly
where a driver will go, instead of drawing a circle that might cross a river
or a state line.

> **The coordinates matter.** Right-click the kitchen in Google Maps and copy
> them. They drive both the delivery-area check and the Uber pickup, so an
> approximate value gives approximate answers.

### Opening hours

Same form, below the delivery area: an open and close time for each weekday,
plus the slot length and how many orders the kitchen can take per slot.

**Delivery slots exist only inside these windows.** Leave both fields of a day
blank to close it. A kitchen with no hours at all saves fine and then tells
every customer "there are no delivery slots available" at the slot step, so
the form warns when that is the case.

| Field | Meaning |
|---|---|
| Open / close | The delivery window for that weekday, in the kitchen's timezone |
| Slot length | How long each bookable window is (default 60 minutes) |
| Orders per slot | How many deliveries the kitchen can handle at once |

A close time earlier than the open time means the day runs past midnight —
`18:00` to `01:00` is a valid late-night kitchen, not an error.

Slots are generated on demand and topped up by a scheduled job, so changing
the hours affects new bookings without disturbing orders already placed.

---

## Chat tester

Talk to the bot from the browser, against the real menu and the real database.
Outbound messages are collected and shown rather than delivered, so **nothing
reaches the live Gallabox account** - it is safe to use on a deployment that
is serving real customers.

- Buttons and list rows appear as chips; clicking one taps it exactly as a
  customer would.
- The panel on the right drops a location pin, which step 9 asks for.
- The header shows the current conversation step.
- **Reset** forgets the test customer so the next message starts at step 1.

Uber and Stripe are not called; the payment step shows the template the
customer would receive.

Two things make the flow stop early, and the page warns about both: no kitchen
configured (stops at the delivery-area check) and no opening hours (no slots
to pick).

---

## The customer ordering page

`/order/{token}` — what the WhatsApp link opens. Worth knowing when answering
a customer question:

| Element | Behaviour |
|---|---|
| Search | Matches dish names *and* category names |
| Category rail | "All Items" plus every category; the ☰ button jumps to one |
| Tapping a dish | Opens full details: image, description, pack size, serves, cuisine |
| `+` / `−` | The count changes on the tap; it is saved to the customer's cart in the background. Fast taps on one dish are sent one at a time, so the saved count is always the last one shown. Up to 20 of a dish |
| Selected dishes | Tinted and outlined in the list |
| Bottom bar | Item count and running total; opens the cart |
| Waiting on the server | Placeholder cards while the menu loads; a spinner on every button that waits (checking delivery, working out the total, placing the order, saving or searching an address); the address sheet opens at once on "Loading your saved addresses…", with Try again if that fails |

**The cart is saved against the customer, not the browser.** Closing the page
and reopening the link restores it, and it is the same cart the WhatsApp bot
sees. Ordering links expire after six hours.

### Brand

The ordering pages, the payment result pages and this dashboard use the Shero
logo and its colours: teal `#029B99`, with the red `#C7010A` of the logo's dot
for small highlights (the Delivery tag, category markers). The palette is
defined once in `app/templates/_brand.html`; the logo files are in
`app/static/brand/`, and the full-size original in `docs/brand/`.

In the dashboard, the sign-in page and the sidebar use the teal gradient
panel; the Dashboard opens on a teal welcome banner with shortcuts. Admin
pages are written with Tailwind's `stone` greys, which `admin/base.html`
redefines as a teal-tinted grey - so a new admin page is on-brand without
extra work. Shared stat tiles, cards, badges and empty states live in
`admin/_macros.html` (`ui.stat` takes an optional `icon` SVG path).

---

## Multi-instance note

A setting saved on one instance reaches other instances within a minute
(the refresh job), not instantly. With a single instance this never comes up.
