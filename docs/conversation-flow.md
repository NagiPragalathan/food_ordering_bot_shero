Written for: engineers working on the bot, and reviewers checking it against the spec.

# Conversation flow

Every step from section 1 of the spec, with where it lives in the code.

## Step map

| # | Step | Conversation step | Handler |
|---|---|---|---|
| 1-2 | Entry, returning check | `START` | `onboarding.start` |
| 3 | Ask name | `AWAIT_NAME` | `onboarding.handle_name` |
| 4 | Ask email | `AWAIT_EMAIL` | `onboarding.handle_email` |
| 5 | Main menu | `MAIN_MENU` | `onboarding.handle_main_menu` |
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
| Invalid email format | Re-ask, stay on `AWAIT_EMAIL` |
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
| Unpaid after 30 min | Link expires, slot released, `payment_expired` template |
| Payment failed | `payment_failed` template with a retry link |

## Reply id conventions

Interactive replies carry a prefixed id so a stale button from an earlier step
is recognisable rather than misread. All defined in `prompts.py`.

| Prefix / id | Meaning |
|---|---|
| `menu:order`, `menu:talk` | Main menu |
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

Set locally first, then mirrored to Zoho. A repeated stage keeps its original
timestamp, so drop-off reports show when a customer *first* reached a point.

| Stage | Set at |
|---|---|
| New Enquiry | First message |
| Details Captured | Name + email |
| Cuisine Selected | Cuisine chosen |
| Cart Created | Cart sent |
| Not Serviceable | Step 6a or 10 |
| Outlet Selected | Kitchen confirmed as serviceable |
| Slot Selected | Slot picked |
| Payment Link Sent | Summary confirmed |
| Payment Abandoned | Link expired |
| Payment Failed | Stripe reported failure |
| Converted | Payment succeeded |

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
bot sends link -> /order/{token}            the page
                  /order/{token}/menu       all 287 dishes, grouped
                  /order/{token}/cart       GET the saved cart
                  /order/{token}/cart       POST one dish to an exact quantity
                  /order/{token}/cart/clear POST to empty it
                  /order/{token}/address    serviceability + bookable slots
                  /order/{token}/quote      priced summary with delivery
                  /order/{token}/confirm    creates the order, sends the pay link
```

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

## Adding a step

1. Add the value to `ConversationStep` in `app/db/models/enums.py`.
2. Write the handler in `app/services/conversation/handlers/`.
3. Register it in `STEP_HANDLERS` in `engine.py`.
4. Put the copy in `prompts.py`.
5. If it captures free text, add it to `FREE_TEXT_STEPS` so global keywords
   do not swallow the answer.
6. Add it to the table at the top of this document.
