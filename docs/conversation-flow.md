Written for: engineers working on the bot, and reviewers checking it against the spec.

# Conversation flow

Every step from section 1 of the spec, with where it lives in the code.

## Step map

| # | Step | Conversation step | Handler |
|---|---|---|---|
| 1-2 | Entry, returning check. A known customer's "hi" / "hello" / "menu" gets one message: "Welcome back to Shero Home Food, {name}! 👋 What would you like to do?" with **Order Now**. The name comes from our own database, not Zoho | `START` | `onboarding.start` |
| 3 | Ask name | `AWAIT_NAME` | `onboarding.handle_name` |
| 4 | Ask email, with a **Change name** button | `AWAIT_EMAIL` | `onboarding.handle_email` |
| 5 | Main menu (one button: **Order Now**) | `MAIN_MENU` | `onboarding.handle_main_menu` |
| 6 | Cuisine + Check Availability | `CUISINE_MENU` | `menu.handle_cuisine_menu` |
| 6a | Check Availability | `AWAIT_AVAILABILITY_LOCATION` | `menu.handle_availability_location` |
| 7a | Categories (paged) | `BROWSING_CATEGORIES` | `menu.handle_categories` |
| 7b | Dishes (paged) | `BROWSING_ITEMS` | `menu.handle_items` |
| 7c | Quantity | `AWAIT_QUANTITY` | `menu.handle_quantity` |
| 8 | Cart review | `CART_REVIEW` | `menu.handle_cart_review` |
| 9 | Location | `AWAIT_LOCATION` | `location.handle_location` |
| 10 | Serviceability | — | `location.confirm_service` |
| ~~11~~ | ~~Nearby outlets~~ | — | **Removed — single kitchen** |
| 12 | Delivery details | `AWAIT_ADDRESS` → `AWAIT_APARTMENT` → `AWAIT_INSTRUCTIONS` → `AWAIT_CONTACT_NUMBER` | `delivery.*` |
| 13 | Delivery slot | `AWAIT_SLOT_CHOICE` | `delivery.handle_slot_choice` |
| 14 | Order summary | `AWAIT_SUMMARY_CONFIRM` | `checkout.handle_summary_choice` |
| 15 | Payment link | `AWAIT_PAYMENT` | `payments.create_payment_link` |
| 16 | Payment result | — | `webhooks_stripe` → `payments.handle_payment_*` |
| 17 | Kitchen alert | — | `payments.notify_kitchen` |
| 18 | Delivery updates | — | `POST /ops/orders/{n}/…` |
| 19 | Feedback | `AWAIT_FEEDBACK` | `feedback.handle_feedback` |

Terminal steps: `HANDED_OVER` (with an agent, bot stays quiet), `COMPLETED`
(next message restarts).

## Failure paths

Each matches the spec's "If it fails" column.

| Situation | Behaviour |
|---|---|
| Empty/invalid name | Re-ask, stay on `AWAIT_NAME` |
| Invalid email format | Re-ask, stay on `AWAIT_EMAIL` (Change name button still offered) |
| Wrong name noticed at the email step | Tap **Change name** - back to `AWAIT_NAME`; the new name replaces the old |
| "Talk to Us" | Hand to a Gallabox agent, bot goes quiet |
| Not serviceable | Polite message, Zoho stage `Not Serviceable`, end |
| Item unavailable | Name the item, re-show the menu |
| Location unreadable | Re-ask for a pin or ZIP |
| Invalid quantity | Re-ask for a number between 1 and 20 |
| Details incomplete at slot time | Restart the step-12 questions |
| No slots today | Search rolls forward to the next available day |
| Slot taken between listing and picking | "Pick another one", re-show slots |
| Uber quote fails | Block the summary rather than guess a delivery fee |
| Edit at summary | Abandon draft, release slot, back to step 7 |
| Cancel at summary | Abandon draft, release slot, close politely |
| Unpaid after 15 min | `payment_reminder` template |
| Unpaid after 30 min | Link expires, slot released, dishes put back in the cart, `payment_expired` template. Its **Order Now** button opens the menu with that cart (`/pay/<order>` redirects a dead order to a fresh menu link) |
| Order changed (Change menu / Update location) | Old order cancelled and its link marked expired, so neither the 15- nor the 30-minute message is sent for it |
| Payment failed | `payment_failed` template with a retry link |
| Voice note, photo, sticker, video, file or contact card | "I can only read typed messages and button taps", plus what to do at this step; the step does not change. As the very first message it gets the normal welcome |
| Anything a step did not match and so sent no reply | The engine notices nothing was sent and replies "Sorry, I did not understand that" (at a name/email/address step: "please type your answer") |
| Reaction (👍) or a Gallabox status event | No reply, on purpose |

### The bot is never silent

After every message the engine checks whether the step handler sent the
customer anything (`FlowContext.sent`). If it sent nothing, the engine replies
itself and logs `handler_sent_nothing` with the step, so a gap in a handler
shows up in the logs instead of as a customer waiting on silence. The one
deliberate silence is a chat handed to a human agent (`HANDED_OVER`): the agent
answers there.

## Reply id conventions

Interactive replies carry a prefixed id so a stale button from an earlier step
is recognisable rather than misread. All defined in `prompts.py`.

| Prefix / id | Meaning |
|---|---|
| `menu:order` | Main menu's only button, **Order Now** |
| `menu:talk` | Retired from the main menu; still honoured so a stale button works |
| `email:change_name` | Change name, under the email question |
| `link:new` | Get new link on the old separate message (still honoured); the `menu_link` template's quick reply arrives as the text "Get new link" |
| `cuisine:<name>` | Cuisine choice |
| `cuisine:check_availability` | Check Availability |
| `cat:<slug>` | Category choice |
| `item:<retailer_id>` | Dish choice |
| `page:<n>` | Next page of a long list |
| `nav:cuisines` / `nav:categories` | Go back a level |
| `cart:checkout` / `cart:more` / `cart:clear` | Cart review buttons |
| `slot:<uuid>` | Slot choice |
| `summary:confirm` / `edit` / `cancel` | Summary buttons |
| `rating:great` / `good` / `poor` | Feedback |

## Lead stages

Set locally first, then mirrored to the customer's Zoho **Lead**: Bot Stage
and Bot Stage History (see [zoho-setup.md](zoho-setup.md)). A repeated stage
keeps its original timestamp, so drop-off reports show when a customer
*first* reached a point.

| Stage | Set at (chat) | Set at (web page) |
|---|---|---|
| New Enquiry | First message | |
| Details Captured | Name + email | |
| Cuisine Selected | Cuisine chosen | |
| Cart Created | Cart sent | First dish added; also when Change menu / Update location releases an order |
| Not Serviceable | Step 6a or 10 | Address outside the delivery area |
| Outlet Selected | Kitchen confirmed as serviceable | Address checked (also pushes street, unit, ZIP, kitchen and distance to Zoho) |
| Slot Selected | Slot picked | Total shown for a picked time |
| Payment Link Sent | Summary confirmed | Confirm & continue on WhatsApp |
| Payment Abandoned | Link expired | |
| Payment Failed | Stripe reported failure | |
| Converted | Payment succeeded | |

The web page only moves a customer **forward** through the funnel
(`crm_sync.FUNNEL`), so adding a dish after picking a time does not rewind
the customer to Cart Created. A stage outside the funnel (Not Serviceable, Abandoned,
Failed, Converted) ends an attempt, and the next one starts wherever it
starts.

On payment the Lead's Bot Stage becomes **Converted** and the order is filed
in the Orders module, linked to the Lead (dishes, totals, slot, address,
status). That is the spec's "Lead -> Contact" step: in Shero's CRM a Contact
is a Kitchen Partner, so the Lead is not converted.

## Two ways to browse (`ORDER_MODE`)

| Mode | What the customer gets |
|---|---|
| **`web`** (default) | A personal link to the storefront. Menu, cart, address, slot and summary all happen on the page; the payment link comes back to WhatsApp. |
| `chat` | The in-WhatsApp list browsing described below. |

### The web storefront

`GET /order/{token}` — the token is a signed, expiring value carrying the
customer id, so there is no login and the URL cannot be edited to order as
somebody else. Links last six hours; an old one renders an expired page.

```
bot sends button -> /order/{token}          the page
                  /order/{token}/menu       all 287 dishes, grouped
                  /order/{token}/cart       GET the saved cart
                  /order/{token}/cart       POST one dish to an exact quantity
                  /order/{token}/cart/clear POST to empty it
                  /order/{token}/addresses  GET saved addresses, POST add/edit one
                  /order/{token}/addresses/{id}/delete
                  /order/{token}/locate     street + ZIP at a map pin
                  /order/{token}/address    serviceability + bookable slots
                  /order/{token}/quote      priced summary with delivery
                  /order/{token}/confirm    creates the order, sends the pay link
```

#### Keeping the page fast on a phone

- **Compression.** Responses over 1 KB are gzipped (`GZipMiddleware`): the
  287-dish menu JSON goes from ~166 KB to about a fifth of that.
- **Thumbnails.** Dish cards and the cart use `thumb_url`
  (`/media/thumb/<name>`, 240 px, ~13 KB; `services/media_thumbs.py`). The
  full photo (`image_url`, up to 800 px, ~89 KB) is only loaded when a dish is
  opened. All 287 thumbnails are 3.8 MB against 25.7 MB of full photos. They
  are made on first request and kept in `data/media/thumbs/`.
- **Browser caching.** Photos and thumbnails are sent with
  `Cache-Control: public, max-age=31536000, immutable`
  (`core/http_cache.py`). Their names are content digests, so a changed photo
  is a new URL; a returning customer downloads no photos at all.

#### Saved addresses (Home, Office, ...)

A customer can keep several delivery addresses (up to 10), each with a label:
Home, Office, or a name of their own ("Mum's place"). The page lists them,
pre-selects the last one ordered to, and offers **+ Add a new address**, **Edit**
and **Delete** (`app/services/addresses.py`, table `customer_addresses`).

- **Map pin.** The add/edit form has a search box and a map with a fixed pin
  in the middle. The customer searches the address (`/search`), moves the
  map under the pin, or taps **Use my current location**; the street and ZIP
  are filled in from the pin (`/locate`) and stay editable. A pinned address
  is checked for delivery by its exact point; a typed one by its ZIP. If the
  map cannot load, the form still works as plain fields.
- **Google.** For accurate US house numbers, set both Google keys:

  | Setting | Used for | Where it runs | Restrict it in Google Cloud to |
  |---|---|---|---|
  | `GOOGLE_MAPS_API_KEY` | pin -> street, search -> pin, ZIP lookups | server only | the Geocoding API (IP restriction if the server IP is fixed) |
  | `GOOGLE_MAPS_BROWSER_KEY` | drawing Google's map, with satellite view | the customer's browser (public) | the Maps JavaScript API, HTTP referrer = your domain |

  Either can be pasted in the admin **Settings -> Maps** section and applies
  within a minute. Without the server key, lookups use OpenStreetMap
  (Nominatim), which often lacks house numbers. Without the browser key, an
  OpenStreetMap map is drawn instead. Reverse lookups ask Google for a real
  street address first and keep the customer's own pin as the point.
- **Contact number.** Shown, not editable: the driver calls the WhatsApp
  number the customer is chatting from. The server ignores any contact
  number the browser sends.
- **Ownership.** `/address`, `/quote` and `/confirm` take an `address_id`,
  which is looked up only among the link owner's addresses.
- **Carried over.** A customer who already had an address on file finds it
  listed as "Home" the first time; nobody retypes it.
- The address ordered to is copied onto the customer record too, because
  the WhatsApp flow, the CRM and the delivery quote read it from there.

#### After ordering

1. After a delivery time is picked, **See the total** shows the items, the
   charges, the delivery address and the time.
2. **Confirm & continue on WhatsApp** creates the order and sends the
   `order_summary` template (items, address, time, total) with **Pay Now**,
   **Change menu** and **Update location**. Until Meta approves it, the
   approved `payment_link` goes instead, so a customer can always pay.
   With no `STRIPE_SECRET_KEY` the checkout is mocked: the summary is still
   sent, and Pay Now opens a "Test payment" page (`/pay/mock/<order>`)
   whose **Simulate successful payment** runs the real success path.
3. The page shows "Order placed" and opens
   `https://wa.me/<WHATSAPP_BUSINESS_NUMBER>` by itself; **Go to WhatsApp**
   and **Pay here instead** stay on screen in case the browser does not
   follow. With the setting empty, the page stays put.

**Change menu** / **Update location** (`handlers/order_changes.py`,
`services/order_changes.py`) release the unpaid order - slot hold, and the
Stripe link, so the old Pay Now stops working - restore its dishes to the
cart and send a fresh link. Update location's link is an **Update address**
button to the address-only page `/order/<token>/location`
(`order/location.html`): the dishes, then the saved addresses and map, the
time, the total and Confirm. There is no menu on it; a "Change dishes
instead" link goes to the menu. It shares the address book
(`_address_book.html`), the checkout steps (`_checkout.html`) and the sheet
(`_sheet.html`) with the menu page. A paid order is not touched.

### One cart, stored in the database

The cart lives in `conversations.context["cart"]` and is managed by
`services/cart.py`. **The WhatsApp handlers and the web page write to the same
list**, so closing the page and reopening the link restores the cart, and a
dish added in chat is already there on the page.

`POST /cart` sets a dish to an *exact* quantity rather than incrementing, so a
double-tap or a retried request cannot silently add it twice.

**The browser is never trusted with names or prices.** It sends a retailer id
and a quantity; the name and price are read from the menu, unavailable dishes
are refused, and `quote` and `confirm` re-read the cart from the database
rather than from the request body — a tab left open for an hour cannot price a
cart the customer has since changed.

On confirm the conversation is parked on `AWAIT_PAYMENT`, so the reminder,
expiry and feedback jobs treat a web order identically to one placed in chat.

If the payment link cannot be created, the draft order is cancelled and its
slot released rather than left reserved against an order nobody can pay for.

## Browsing a 287-dish menu in WhatsApp (`ORDER_MODE=chat`)

A WhatsApp list message holds **10 rows**, so the menu cannot be one list.
It is navigated in three levels, with one row reserved for paging:

```
Cuisine list        Chettinad / Kerala / Andhra + Check Availability
  -> Categories     9 per page, then "More options"
    -> Dishes       9 per page, with price, pack size and serving count
      -> Quantity   a number, 1-20
        -> Cart     Checkout / Add more / Clear cart
```

Typing a dish name at any browsing step runs a **search** instead
(`menu._try_search`), which is faster than tapping through for someone who
knows what they want.

Because the menu is ours rather than Meta's, there is no native WhatsApp cart;
the bot keeps the cart in the conversation context. If the Meta catalogue is
populated later, an incoming native cart is still accepted and adopted
(`engine._adopt_native_cart`).

## The Check Availability shortcut

Step 6a is optional but has a real effect: a location captured there is saved
on the customer, so when the cart arrives, **step 9 is skipped entirely** and
the flow goes straight to outlet selection. `location.request_or_resume`
decides this. There is a test for it
(`test_check_availability_saves_location_and_skips_step_nine`).

## The menu link is a button

The menu goes out as **one message** with two buttons, **View Menu** and **Get
new link**: the `menu_link` template. A plain link message (`cta_url`) may
carry only its one button, so a template - which can mix a URL button with
quick replies - is the only way to have both on one message. View Menu opens
`https://<PUBLIC_BASE_URL>/order/<token>` in WhatsApp's in-app browser; the
86-character token is the button parameter.

`send_order_link` falls back in two steps, so nobody is left without a menu:

1. `menu_link` not approved -> a single `cta_url` **View Menu** message goes
   instead. Its footer reads "Link expired? Just type: new link". Approval is
   **checked with Gallabox before sending** (`template_status.is_approved`,
   cached five minutes): Gallabox accepts a send for an unapproved template
   and it then fails silently, so waiting for a send error would leave the
   customer with nothing. A send error still falls back too
   (`menu_link_template_failed`). `order_summary` works the same way, with
   `payment_link` as its fallback.
2. `cta_url` refused too -> logged `order_link_button_failed`, and the link
   is sent as plain text.

A message with its own wording (Change menu, Update location) cannot use the
fixed template text, so it always goes as the `cta_url` message.

`menu_link`'s URL is baked into the approved template: moving to a new
domain means submitting it again, like `payment_link` and `order_summary`.

### Getting a new link

Links last six hours. There are three ways to get a fresh one, and all of
them go through `show_cuisines`, so `ORDER_MODE` still decides what is sent:

1. **Get new link button** on the `menu_link` template. A quick reply comes
   back as its label, which is in `NEW_LINK_KEYWORDS`. It works from any
   step. A `link:new` button, from the separate message sent before the
   template existed, is still honoured.
2. **The expired page.** Opening an old link shows "This menu link has
   expired" and a **Get a new link on WhatsApp** button: `wa.me/<business
   number>?text=New menu link`. The customer taps send and the bot replies
   with a fresh link. It needs `WHATSAPP_BUSINESS_NUMBER`; without it the page
   says to send "new menu link" instead.
3. **A tab left open.** Once the link dies, every page call answers
   `{"expired": true, "new_link_url": ...}` and the page swaps to the same
   screen, rather than showing one error per tap. The cart is kept in the
   database, so the new link opens it again.

Typed words that ask for a link: `new menu link`, `new link`, `menu link`,
`get new link`, `new menu` (not at name, email or address questions, where
typed text is the answer). Someone with no name or email yet is onboarded
first.

## Messages switched off

| Message | Setting | What still happens |
|---|---|---|
| `payment_reminder` (15 min unpaid) | `SEND_PAYMENT_REMINDER=false` | The link still expires at 30 minutes and still releases its slot |
| `order_out_for_delivery`, `order_delivered` | `SEND_DELIVERY_UPDATES=false` | Orders still move through the stages, and the feedback request still fires |

Both are off at the client's request. The templates stay approved on the WABA,
so turning one back on is the flag and nothing else - no resubmission.

## Opening a conversation (outreach)

Every other path in this document answers somebody. `services/outreach.py` is
the only one that speaks first, and it is the only place where the customer
may not exist yet.

```bash
python -m scripts.send_welcome 917401268091
```

`send_welcome()`:

1. Creates the lead if there is none, so the reply has somewhere to land and
   the customer is in the funnel from the moment we contacted them.
2. Greets them with `customer.greeting_name` - the name if we have one,
   otherwise **"there"**. A template renders a missing parameter as an empty
   string, so without this the message reads "Hi , welcome to Shero".
3. Sends `shero_welcome`, whose **Order Now** button carries them into the
   ordinary flow.
4. Refuses to send (`AlreadyWithAgent`) if one of your team is mid-conversation
   with that person.

Because the conversation is still at `START`, the tap runs `onboarding.start`,
which sees no name and asks for it. **A lead we contacted first is always asked
its name before anything else.** Zoho being unreachable logs a warning and does
not stop the greeting.

> `customer.greeting_name` is used by every template that greets by name -
> `payment_link`, `payment_reminder`, `payment_failed`, `payment_expired` -
> so the rule lives in one place rather than four.

## Adding a step

1. Add the value to `ConversationStep` in `app/db/models/enums.py`.
2. Write the handler in `app/services/conversation/handlers/`.
3. Register it in `STEP_HANDLERS` in `engine.py`.
4. Put the copy in `prompts.py`.
5. If it captures free text, add it to `FREE_TEXT_STEPS` so global keywords
   do not swallow the answer.
6. Add it to the table at the top of this document.
