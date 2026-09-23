"""Every line the bot says, in one place.

Kept out of the handlers so the client can review and reword the copy without
touching flow logic, and so the whole script can be translated later by
swapping this module.

These are in-conversation replies, which do not need Meta approval. Only the
10 template messages in `integrations/gallabox/templates.py` do.
"""

from __future__ import annotations

# --- Reply ids -------------------------------------------------------------
# Prefixed so a handler can tell what kind of thing was tapped, and so a
# stale button from an earlier step is recognisable rather than misread.
MENU_ORDER = "menu:order"
MENU_TALK = "menu:talk"
CHECK_AVAILABILITY = "cuisine:check_availability"
CUISINE_PREFIX = "cuisine:"
CATEGORY_PREFIX = "cat:"
ITEM_PREFIX = "item:"
PAGE_PREFIX = "page:"
CART_CHECKOUT = "cart:checkout"
CART_ADD_MORE = "cart:more"
CART_CLEAR = "cart:clear"
BACK_TO_CUISINES = "nav:cuisines"
BACK_TO_CATEGORIES = "nav:categories"
SLOT_PREFIX = "slot:"
SUMMARY_CONFIRM = "summary:confirm"
SUMMARY_EDIT = "summary:edit"
SUMMARY_CANCEL = "summary:cancel"
RATING_PREFIX = "rating:"

SKIP_TOKENS = {"skip", "none", "no", "-", "na", "n/a"}

# --- Onboarding (steps 3-5) --------------------------------------------------
WELCOME_ASK_NAME = (
    "Welcome to Shero Home Food! \U0001F35B\n\n"
    "Authentic home-style meals, delivered.\n\n"
    "To get started, what is your full name?"
)
NAME_REASK = "Sorry, I did not catch that. Please type your full name."
ASK_EMAIL = "Thanks {name}! What is your email ID? We will send your receipt there."
EMAIL_REASK = (
    "That does not look like a valid email address. "
    "Please enter it again, for example asha@example.com"
)
WELCOME_BACK = "Welcome back, {name}! \U0001F44B"
MAIN_MENU = "What would you like to do?"
BTN_ORDER_ONLINE = "Order Online"
BTN_TALK_TO_US = "Talk to Us"

HANDOVER = (
    "Sure - connecting you to a member of our team. "
    "Someone will reply here shortly."
)

# --- Cuisine and availability (steps 6, 6a) ----------------------------------
CUISINE_PROMPT = "Which cuisine would you like today?"

# Web ordering (ORDER_MODE=web). A WhatsApp list holds ten rows, so a 287-dish
# menu is sent as a link to the storefront instead of paged chips.
ORDER_LINK_PROMPT = (
    "Here is our full menu 🍛\n\n"
    "Browse all our dishes, add what you like to your cart, choose a delivery "
    "time and we will send your payment link right back here.\n\n"
    "{url}\n\n"
    "_The link is personal to you and works for the next few hours._"
)
ORDER_LINK_FOLLOWUP = (
    "Tap the link above to build your order. Send *menu* if you need a fresh link."
)
CUISINE_LIST_BUTTON = "View options"
CHECK_AVAILABILITY_LABEL = "Check Availability"
CHECK_AVAILABILITY_DESC = "See if we deliver to your area"

ASK_AVAILABILITY_LOCATION = (
    "Share your location pin, or type your ZIP code, "
    "and I will check if we deliver to you."
)
AVAILABILITY_OK = (
    "Good news - we deliver to you! \U0001F389\n\n"
    "Your order will be cooked at {kitchen}{distance}."
)
NOT_SERVICEABLE = (
    "Sorry, we do not deliver to your area yet. \U0001F61E\n\n"
    "We are expanding quickly, so please do check back with us soon."
)
LOCATION_UNREADABLE = (
    "I could not read that location. Please share your location pin, "
    "or type a valid ZIP code."
)

# --- Menu and cart (steps 7-8) -----------------------------------------------
# The menu runs to hundreds of dishes and a WhatsApp list holds 10 rows, so it
# is browsed cuisine -> category -> dish, with paging.
CATEGORY_PROMPT = "{cuisine} — what would you like?"
CATEGORY_LIST_BUTTON = "Browse dishes"
ITEM_PROMPT = "{category} — tap a dish to add it:"
ITEM_LIST_BUTTON = "Choose a dish"
MORE_LABEL = "More options"
MORE_DESC = "Show the next page"
BACK_TO_CUISINES_LABEL = "Other cuisines"
BACK_TO_CATEGORIES_LABEL = "Back to categories"

MENU_EMPTY = (
    "Sorry, there are no {cuisine} dishes available right now. "
    "Please pick another cuisine."
)
ASK_QUANTITY = "How many *{item}* would you like? ({price} each)\n\nReply with a number."
QUANTITY_REASK = "Please reply with a number between 1 and {max}."
ITEM_ADDED = "Added {quantity} x {item}. \U0001F6D2"

CART_EMPTY = "Your cart is empty. Add a dish to get started."
CART_HEADER = "*Your cart*"
CART_PROMPT = "What would you like to do next?"
BTN_CHECKOUT = "Checkout"
BTN_ADD_MORE = "Add more"
BTN_CLEAR_CART = "Clear cart"
CART_CLEARED = "Cart cleared."
SEARCH_NO_RESULTS = (
    'No dishes matched "{term}". Try another word, or tap a category.'
)

# --- Location and serviceability (steps 9-10) --------------------------------
ASK_LOCATION = (
    "Almost there! Share your location pin, or type your ZIP code, "
    "so we can check delivery and work out the charge."
)
SERVICEABLE_CONFIRMED = "Great - we deliver to you! \U0001F389"

# --- Delivery details (step 12) ----------------------------------------------
ASK_ADDRESS = "What is your full delivery address? (street and house number)"
ASK_APARTMENT = "Apartment, unit or floor? (type 'skip' if not applicable)"
ASK_INSTRUCTIONS = "Any delivery instructions for the driver? (type 'skip' if none)"
ASK_CONTACT_NUMBER = (
    "Which number should the driver call? "
    "Send 'same' to use this WhatsApp number."
)
CONTACT_REASK = (
    "That does not look like a valid phone number. "
    "Please enter it again, or send 'same' to use this WhatsApp number."
)
ADDRESS_REASK = "Please enter a delivery address of at least a few characters."

# --- Slots (step 13) ---------------------------------------------------------
SLOT_PROMPT = "Pick a delivery slot:"
SLOT_LIST_BUTTON = "Choose a slot"
NO_SLOTS = (
    "Sorry, there are no delivery slots available right now. "
    "Please try again a little later."
)
SLOT_TAKEN = "Sorry, that slot was just taken. Please pick another one."

# --- Summary and payment (steps 14-15) ---------------------------------------
SUMMARY_PROMPT = "Please review your order:"
BTN_CONFIRM = "Confirm"
BTN_EDIT = "Edit"
BTN_CANCEL = "Cancel"
PAYMENT_SENDING = "Great! Sending your secure payment link now..."
ORDER_CANCELLED = "No problem - your order has been cancelled. Message us any time!"
QUOTE_FAILED = (
    "We could not calculate the delivery charge just now. "
    "Please try again in a moment."
)

# --- Feedback (step 19) ------------------------------------------------------
FEEDBACK_THANKS = "Thank you for the feedback! \U0001F64F"

# --- Generic -----------------------------------------------------------------
FALLBACK = (
    "Sorry, I did not understand that. "
    "Please use the buttons above, or type 'menu' to start again."
)
GENERIC_ERROR = (
    "Something went wrong on our side. Please try again in a moment, "
    "or type 'agent' to talk to our team."
)
NO_KITCHEN = (
    "We are not taking orders just now. Please try again shortly."
)
RESTART_KEYWORDS = {"menu", "start", "restart", "hi", "hello", "hey"}
AGENT_KEYWORDS = {"agent", "human", "support", "help", "talk to us"}


def format_cart(lines: list[dict], currency_symbol: str = "$") -> str:
    """Render the working cart for the review message (spec step 8)."""
    if not lines:
        return CART_EMPTY

    rows = [CART_HEADER, ""]
    total = 0.0
    for line in lines:
        quantity = int(line.get("quantity", 1))
        price = float(line.get("unit_price", 0))
        line_total = quantity * price
        total += line_total
        rows.append(
            f"{quantity} x {line.get('name', 'Item')} "
            f"- {currency_symbol}{line_total:.2f}"
        )

    rows += ["", f"Subtotal: {currency_symbol}{total:.2f}",
             "_Delivery and taxes are added at checkout._"]
    return "\n".join(rows)
