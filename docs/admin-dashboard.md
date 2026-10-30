Written for: the Shero team who will run the bot day to day.

# Admin dashboard

`https://api.<yourdomain>/admin/` — or `http://localhost:8000/admin/` when
running locally.

Screens in the sidebar: **Dashboard, Menu, Orders, Kitchens, Uber queue,
Customers, Bot replies, Settings**.

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
  a sheet import, or after editing kitchens. It is greyed out
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

An order placed with a coupon shows the code and the amount taken off under
its total.

---

## Offers

Coupon codes customers apply at checkout (**Offers** in the sidebar, at
`/admin/promotions`). The top of the page shows how many offers are live, how
many times codes have been redeemed, the discount given and the sales those
orders brought in. Each offer is a card with its status (Live, Scheduled,
Paused, Expired, Limit reached), its uses, and an on/off switch.

### Creating an offer

**Add promotion** opens the editor. On the right, a preview shows the offer
the way customers see it, and **Try it on an order** shows what a sample cart
would pay.

| Field | What it does |
|---|---|
| Title, Offer description | Shown to customers, and the title on the bill |
| Coupon code | What customers type, any case. **Generate** makes one up |
| Offer tag | The banner label, e.g. `FLAT 20% OFF`. Blank uses one made from the discount |
| Discount type | **Percentage** or **Flat amount** off the dishes, or **Free delivery** (the delivery charge is waived) |
| Max discount | Caps a percentage, e.g. 20% off up to $10 |
| Min order | Dish subtotal needed. Below it the customer is told "Add $X more" |
| Total uses | Across everyone, e.g. the first 100 orders. Blank: unlimited |
| Uses per customer | 1 = once each. Blank: unlimited |
| First order only | Only customers who have never paid for an order |
| Kitchen | Only orders cooked by one kitchen |
| Start / End date | Whole days in the kitchen's timezone, both included |
| Where customers see it | **Menu banner + checkout**, **Checkout only**, or **Hidden** (works only when typed, for codes you hand out) |
| Display order, Active | Listing order; untick Active to pause |

### How a use is counted

A use counts when the order is **paid**. An unpaid order holds its use only
while its 30-minute payment link is open, so the last use of a limited code
cannot go to two people; an expired or changed order gives it back, and so
does a refund. A customer who places a new order with the same code replaces
their older unpaid one.

### What the customer sees

- Offers marked **Menu banner** appear as cards above the menu, with Apply.
- The cart shows the applied code, or how much more unlocks the nearest offer.
- The order summary has a coupon box, the listed offers with what each saves
  (or why not yet), and the discount as its own line in the total.
- In the WhatsApp chat (ordering inside WhatsApp, not on the page), the
  customer types **offers** to see the codes they can use, **apply CODE**
  (or just the code) to use one, and **remove coupon** to take it off. A code
  typed at the order summary re-prices it at once on the same delivery slot;
  the summary's footer says "Have a coupon? Type the code." while none is on
  it. Every rule above is checked exactly as on the page, and the two share
  one applied code. Once the payment link is out the total is fixed, so the
  customer is told to type **change menu** first. Hidden codes are never
  listed but work when typed. A code is not read from a free-text answer
  (name, address and so on).
- When the loyalty rules do not allow points and a coupon on one order, the
  customer's latest choice wins: a coupon typed after points keeps the
  coupon and tells them their points stay in their balance; **redeem** after a
  coupon tells them to remove the coupon first.
- The WhatsApp summary and payment messages give the total with the saving,
  e.g. `$44.59 (you saved $8.87 with FEAST20)`.
- Stripe shows the same discount line, and the PDF bill lists the coupon and
  "You saved".

Editing or deleting an offer never changes an order already placed: each
order keeps its own copy of the code, title and amount.

---

## Uber queue

**Finding an order:** status tabs with counts (All, Waiting, Booked, Retrying, Missed, Cancelled), a search box (order number, customer name or number, delivery address or ZIP) and a delivery-day filter (Today, Upcoming, Past - in the kitchen's timezone). Filters are in the address bar, so a filtered view can be bookmarked.

Every paid order joins the queue the moment it is paid, with the time it
will be sent to Uber: **2 hours before its delivery slot starts**
(`UBER_DISPATCH_HOURS_BEFORE`). A job checks every 5 minutes and books the
courier through Uber Direct: pickup from the kitchen from the send time,
delivery inside the customer's slot.

The booking happens whatever the order's stage, including after someone sets
**Out for Delivery** by hand on the Orders page. Only an order already booked,
or marked Delivered, Cancelled or Refunded, is skipped.

**When to book** is set on the Settings page under Business rules (*When to
book the Uber courier*, 0.5 to 12 hours, default 2). Saving a new value also
moves the send time of every order still waiting for its courier, and their
kitchen alerts, so the kitchen always hears first.

**Paused while Stripe is in test mode.** Uber Direct has no sandbox address:
the credentials decide whether a booking is real. With live Uber keys and
Stripe on `sk_test_` keys, a test payment would book a real, paid courier, so
the job books nothing and the queue shows a blue *Automatic booking is
paused* notice. **Send to Uber now** still books (a real courier). Booking
resumes by itself once Stripe has live keys.

| Status | Meaning |
|---|---|
| Waiting | Paid; booked automatically at the time shown |
| Booked | Uber accepted it. **Track courier** opens Uber's tracking page |
| Retrying | Uber refused or was unreachable; the reason is shown and the bot tries again every 5 minutes until the slot ends |
| Missed | The slot ended without a courier - arrange it by hand |
| Cancelled | The booking was cancelled here |

**Send to Uber now** books straight away - to test the connection, or to
rescue a stuck order. **Cancel Uber** cancels a booking. A yellow box at the
top says what is missing when couriers cannot be booked: the Uber keys, or
the kitchen phone number ([Kitchens](#kitchens) -> **Kitchen phone (for the
courier)**; the Kitchen WhatsApp number is used when it is empty).

The Uber keys in `.env` are the **test** keys: bookings are simulated and
no courier comes. Replace them with the production keys from Uber Direct ->
Developer before go-live.

---

## Customers

**Finding a customer:** the numbers at the top (customers, paying, new this week, not in Zoho), then search (name, number - with or without `+` - or email) and filters for stage, orders (has ordered / has paid / no orders yet) and Zoho (in Zoho / not in Zoho), with sorting (last active, newest, oldest, name). 25 a page. A number opens the chat in WhatsApp.

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

Zoho and the business rules. Values saved here **override `.env` and take
effect immediately** — no redeploy.

| Group | What it holds |
|---|---|
| Zoho CRM | Domain dropdown and **Connect Zoho** |
| Order alerts | WhatsApp numbers told about every paid order |
| Business rules | Tax, payment timings, when to book the Uber courier, public URLs |

### Order alerts

The WhatsApp numbers that get a message **the moment an order is paid**: one
row per number (with country code, 10 to 15 digits) and an optional name.
**Add number** adds a row, the x removes one; an empty list turns alerts off.
**Send test alert** sends a message marked `TEST - not a real order` to the
saved numbers.

The alert (template `shero_new_order_alert`) shows the order number, customer
name and phone, amount, delivery slot, kitchen, address and dishes. Until Meta
approves the template it goes as an ordinary message, which WhatsApp only
delivers to a number that messaged the bot in the last 24 hours. A failed
alert never affects the order. Stored as `ORDER_ALERT_NUMBERS`
(`number|name,...`), not read from `.env`; the sending is
`services/order_alerts.py`.

The service keys — WhatsApp (Gallabox), Stripe, Uber Direct, Meta Catalogue
and Google Maps — are **not** on this page: they are read from the server's
`.env` only. A value saved for one of them in the past is ignored (log:
`setting_not_editable_ignored`), so `.env` is the one place to change them.

### Test connection

The Zoho group has a **Test connection** button that makes one real, read-only
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

### Delivery times

- **Earliest delivery:** fixed at 24 hours after the customer orders
  (`SLOT_MIN_LEAD_HOURS` in `.env`, not shown on this page). Someone ordering
  at midnight sees slots from midnight the next day, and only those inside the
  kitchen's hours.
- **Which hours:** set per kitchen on the [Kitchens](#kitchens) page, per
  weekday. The kitchen is set to 9:00 AM - 9:00 PM every day, so slots run
  9-10 AM through 8-9 PM.
- **Courier:** see [Uber queue](#uber-queue) below.

---

## Kitchens

**The list:** numbers at the top (taking orders, open right now, paused, without opening hours), search (name, street, city, ZIP) and a filter (taking orders / paused / open now). Each kitchen shows whether it is open now, today's hours, its delivery area and courier number, with **Edit**, **Pause / Resume** and **Make default**. A paused kitchen is not matched to new customers. The map draws every active kitchen's delivery circle (Google Maps; OpenStreetMap without a browser key). On the form, **Use my location** asks first when it is more than 50 miles from the current pin.

**Kitchens** in the sidebar lists every kitchen, with **Add kitchen** to set up
another. Add as many as you run.

**How a customer is matched.** When a customer shares their location (WhatsApp
pin, a ZIP, or an address on the ordering page), the bot measures the
distance from that point to **every active kitchen**. The address is served
only if it is inside at least one kitchen's delivery area, and the **nearest**
kitchen that covers it gets the order. Outside every area, the customer is told
we do not deliver there yet, and no order can be placed. The chosen kitchen is
locked in at that step: its slots are offered, its address is the Uber pickup,
and it gets the kitchen alert.

The page shows a map of the kitchens with their delivery circles, and
**Check an address**: type any address to see its distance from each kitchen
and which one would get the order.

### Kitchen form

| Field | Meaning |
|---|---|
| Name, street, city, state, ZIP | The pickup address sent to Uber |
| Latitude / longitude | Optional. Leave blank and the address is found on the map when you save. Customer distances are measured from this point; clear both after changing the address so it is found again. |
| Pick on the map | Search an address, or click / drag the pin onto the kitchen door: the street, city, state, ZIP and coordinates fill in by themselves (satellite view available). The map uses `GOOGLE_MAPS_BROWSER_KEY`; the address lookups go through the server's Geocoding key. |
| Timezone | Set from the kitchen's pin when you save (offline lookup, `tzfpy`), so slots, opening hours and the 24-hour notice use the kitchen's local time; the drop-down only counts where the pin has no timezone (at sea). Saving also rebuilds the kitchen's future slots: free slots that no longer fit the hours, slot length or timezone are removed and the right ones created. A slot someone holds or has ordered is never removed. |
| Form layout | Sections for Kitchen, Location, Delivery area, Contact and Opening hours. The map draws the delivery circle and redraws it as the pin or the miles change; the radius has a slider and 5 / 10 / 15 / 20-mile quick picks (10 is the default). Each day has an open / closed switch, and **Copy Monday to all days** fills the week. A summary with the Save button stays beside the form, and leaving with unsaved changes asks first. |
| Delivery area rule | **Within a radius** (straight-line distance), **Only these ZIP codes**, or **Radius AND ZIP list** |
| Delivery radius (miles) | Default 10 miles |
| Kitchen WhatsApp | Gets the new-order alert immediately after payment |
| Kitchen phone (for the courier) | Uber gives this to the courier; empty means the WhatsApp number is used |
| Timezone | Slots and the alert time are in this zone |
| Opening hours | Delivery slots exist only inside these windows, per weekday. Leave a day blank to close it. |
| Slot length / Orders per slot | Each bookable window, and how many deliveries fit in one |
| Taking orders | Untick to stop matching customers to this kitchen; its past orders stay |

A kitchen with no opening hours saves, with a warning: it has no slots, so its
customers are told there are no delivery times. A close time earlier than the
open time means the day runs past midnight (`18:00` to `01:00`).

**Make default** picks the kitchen used where no address has been checked yet
(for example the ordering page header before an address is chosen). Saving a
kitchen also updates its Zoho Vendor when Zoho is connected.

### When the kitchen hears about an order

New paid orders notify the assigned kitchen immediately using its **Kitchen WhatsApp** number and the `shero_kitchen_order` template: the order code, delivery slot, kitchen and dishes with quantities. The partner never gets the customer's name, number or address, nor the amount (Shero arranges the delivery). Approve this template in Gallabox so notifications reach partners outside an active chat window; until it is approved, the kitchen gets the team's `shero_new_order_alert` with the customer, amount and address shown as "-".

**Kitchens > View orders** shows each partner's order queue and active order count. **Orders** can be filtered by partner and shows dishes, quantities, delivery notes, the kitchen's number and notification status. **WhatsApp alert accepted** means the provider accepted the send; it is not a delivery or read receipt. Failed sends stay pending for the kitchen-alert worker, and **Retry kitchen alert** sends them again after the number or integration is fixed. Existing queued orders keep their previously saved alert time; **Send to kitchen** can send them earlier.

See [automatic kitchen allocation](automatic-kitchen-allocation.md) for setup and a testing walkthrough.

## Bot replies

Who the bot answers on WhatsApp:

| Option | Effect |
|---|---|
| **Reply to everyone** | Every customer who messages gets the bot. Use when live. |
| **Reply only to whitelisted numbers** | Only the numbers listed on the page get the bot. Use for testing on the live business number. |

Anyone the bot does not answer is left alone: no reply, no customer record, no
Zoho lead, and their message reaches your team in Gallabox as normal.

- **Status banner** at the top: green *Live* or amber *Test mode*, with who
  last changed it. While in test mode every admin page shows a **Test mode**
  badge in its header, linking here.
- **Whitelist:** one row per number, each with an optional name. **Add
  number** adds a row, the x removes one. Numbers need the country code
  (`917401268091`, 10 to 15 digits); spaces, `+` and dashes are fine, and
  anything shorter or longer is refused with the bad number named.
- **Check a number** says whether the bot would reply to a number right now,
  and as which whitelist entry.
- Saving applies straight away. Switching to Everyone asks for confirmation;
  the whitelist option will not save with no numbers (that would silence the
  bot for everyone); the numbers are kept when you switch to Everyone; leaving
  with unsaved changes asks first.

Set **only** on this page: stored in the database as `BOT_REPLY_MODE`
(`all` / `allowlist`) and `BOT_ALLOWED_NUMBERS` (`number|name,...`). They are
not read from `.env`. With nothing saved, the bot replies to everyone.

### Trigger keywords

Which messages start the bot. Separate from the whitelist above; both apply.

- **Reply to any message** (switch, on by default): every message gets the
  bot, as before.
- Switched **off**, a chat only starts the bot when the message matches a
  **trigger keyword**. Add as many as you like (up to 50), each with a match
  type:

  | Match | Matches | Example |
  |---|---|---|
  | **Exact message** | The whole message is the keyword | `hi` matches "Hi!" but not "hi there" |
  | **Contains the word** | The keyword appears anywhere, as whole words | `order` matches "I want to order", not "reorder" |
  | **Starts with** | The message begins with the keyword | `menu` matches "menu please" |

  Capitals, punctuation and emoji are ignored. A keyword can be a phrase
  (`order food`).
- **Reply with** (per keyword): what the bot answers with.

  | Reply with | The customer gets |
  |---|---|
  | **Both messages** | The welcome with the **Order Now** (website) button, then **Continue on WhatsApp** |
  | **Only Order Now** | Just the welcome with the website button |
  | **Only WhatsApp** | One message: the welcome and the **Continue on WhatsApp** button |

  A new customer is asked their name and email first and then gets the
  chosen reply (it is kept on the chat as `welcome_reply`). With *Reply to any
  message* on, everyone gets both.
- **Always answered**, keyword or not, so ordering never breaks half-way:
  - a customer part-way through an order who wrote in the last 24 hours (they
    have to be able to type their address or email);
  - taps on the bot's own buttons;
  - the bot's own messages, such as "New menu link" from an expired page.
- A trigger on a chat that is not mid-order (new, finished, or quiet for over
  24 hours) starts over from the welcome. A trigger typed mid-order is just
  part of the order.
- Anything else gets no reply, no customer record and no Zoho lead, and
  reaches your team in Gallabox as normal. That includes a message after an
  order is finished ("when will it arrive?") and a chat handed to a person.
- **Test a message** says whether a first message would start the bot, and
  which keyword it matched.
- While keyword mode is on, every admin page shows a **Keywords only** badge.
- Switching **off** with no keywords will not save. Switching back **on**
  keeps the keywords for next time.

Stored in the database as `BOT_REPLY_TRIGGER` (`any` / `keywords`) and
`BOT_TRIGGER_KEYWORDS` (JSON: `[{"keyword": "hi", "match": "exact", "reply": "both"}]`;
a keyword saved without `reply` means both). Not
read from `.env`. The check is `services/reply_triggers.decide`, run by the
WhatsApp webhook after the whitelist.

### New customers' details

The bot always asks a new customer for their name. **Ask for their email**
decides whether it then asks for an email:

- **On** (the default): name, then email, then the menu.
- **Off**: name, then straight to the menu. Nobody is asked for an email,
  including customers who gave a name earlier but no email. Someone who was
  asked just before it was switched off is moved on by their next message
  (an email they type then is still kept).

Payments, receipts and Zoho work without an email. Saved as
`ASK_CUSTOMER_EMAIL` (`on` / `off`), and takes effect at once.

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

## Payouts

**Payouts** in the sidebar sets the split between the kitchen partner and
Shero - the kitchen partner's percentage of the food and Shero's, one split
for every kitchen or each kitchen's own - and lists every paid order for the
chosen dates and kitchen with what the kitchen and Shero get, totals and a
CSV. See the [payouts guide](kitchen-payouts.md).

## Loyalty rewards

**Loyalty** in the sidebar manages earning rules, repeat-order bonuses, membership tiers, redemption limits, coupon stacking and eligible kitchens/dishes. Search customer wallets to inspect history or grant/remove points with an audited reason. The program starts disabled. See the [full loyalty guide](loyalty-program.md) for activation, examples and all controls.
