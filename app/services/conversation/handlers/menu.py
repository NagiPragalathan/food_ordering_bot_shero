"""Cuisine, Check Availability, menu browsing and the cart (steps 6-8).

The menu is this service's own data now, not the Meta catalogue, so there is
no native WhatsApp cart to receive. Instead the bot keeps the cart in the
conversation context and the customer builds it by tapping through:

    cuisine -> category (paged) -> dish -> quantity -> cart review

Paging exists because a WhatsApp list message holds 10 rows and the menu runs
to hundreds of dishes.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.config import settings
from app.core.exceptions import IntegrationError
from app.core.logging import get_logger
from app.db.models import ConversationStep, LeadStage
from app.integrations.gallabox import template_status
from app.integrations.gallabox import templates as tpl
from app.integrations.gallabox.messages import Button, ListRow, ListSection
from app.services import cart as cart_service
from app.services import kitchen as kitchen_service
from app.services import menu as menu_service
from app.services import order_link
from app.services.conversation import prompts as p
from app.services.conversation.context import FlowContext
from app.services.conversation.validators import clean_postal_code
from app.services.crm_sync import push_details

log = get_logger(__name__)

MAX_CUISINE_ROWS = 9      # one row is kept for Check Availability
MAX_QUANTITY = 20


# --- step 6: cuisine ---------------------------------------------------------
async def send_order_link(ctx: FlowContext, *, body: str | None = None,
                          open_at: str | None = None,
                          button: str | None = None) -> None:
    """Send a personal link to the web storefront (ORDER_MODE=web).

    The conversation parks on CUISINE_MENU so that "menu" re-sends a fresh
    link, and so a customer who ignores the link and types a dish name still
    lands in the chat search rather than a dead end.
    """
    try:
        url = order_link.build_url(ctx.customer.id, open_at=open_at)
        suffix = order_link.link_suffix(ctx.customer.id, open_at=open_at)
    except RuntimeError as exc:
        # No signing secret configured - fall back to chat browsing rather
        # than leaving the customer with nothing.
        log.error("order_link_unavailable", error=str(exc))
        await _show_cuisine_list(ctx)
        return

    # The usual message is the menu_link template: View Menu and Get new link
    # together, which a plain link message cannot carry. A message with its
    # own wording (Change menu, Update location) cannot use the fixed
    # template text, so it goes as a plain link message.
    if body is None and await _send_menu_template(ctx, suffix):
        ctx.goto(ConversationStep.CUISINE_MENU)
        return

    # A tappable button beats a bare link: it opens in WhatsApp's own browser,
    # so the customer never leaves the app, and there is nothing to mistype.
    # `cta_url` needs no template but does need the 24-hour window, and not
    # every WhatsApp provider passes it through - so a refusal falls back to
    # the plain link rather than leaving the customer with no menu at all.
    try:
        await ctx.reply_cta_url(
            body or p.ORDER_LINK_BUTTON_BODY,
            url=url,
            display_text=button or p.ORDER_LINK_BUTTON_LABEL,
            footer=p.ORDER_LINK_FOOTER,
        )
        log.info("order_link_sent", customer_id=str(ctx.customer.id), kind="button")
    except IntegrationError as exc:
        log.warning("order_link_button_failed", error=str(exc))
        await ctx.reply_text(p.ORDER_LINK_PROMPT.format(url=url))
        log.info("order_link_sent", customer_id=str(ctx.customer.id), kind="text")

    ctx.goto(ConversationStep.CUISINE_MENU)


async def _send_menu_template(ctx: FlowContext, suffix: str) -> bool:
    """The menu_link template. False if it is not approved or could not be sent.

    Approval is checked first: Gallabox accepts a send for an unapproved
    template and it then fails silently, so a send error alone would leave
    the customer with nothing. On False the caller sends the plain View Menu
    message, whose footer tells the customer to type "new link".
    """
    if not await template_status.is_approved(tpl.MENU_LINK.name):
        log.info("menu_link_not_approved_using_fallback")
        return False
    try:
        await ctx.reply_template(tpl.MENU_LINK, button_value=suffix)
    except IntegrationError as exc:
        log.warning("menu_link_template_failed", error=str(exc))
        return False
    log.info("order_link_sent", customer_id=str(ctx.customer.id), kind="template")
    return True


async def show_cuisines(ctx: FlowContext) -> None:
    """Cuisine list plus the optional Check Availability entry."""
    cuisines = await menu_service.list_cuisines(ctx.session)

    if not cuisines:
        log.warning("no_menu_loaded")
        await ctx.reply_text(p.NO_KITCHEN)
        ctx.goto(ConversationStep.COMPLETED)
        return

    if settings.order_mode == "web":
        await send_order_link(ctx)
        return

    await _show_cuisine_list(ctx)


async def _show_cuisine_list(ctx: FlowContext) -> None:
    """Browse in chat (ORDER_MODE=chat), and the fallback when no link can
    be signed."""
    cuisines = await menu_service.list_cuisines(ctx.session)

    rows = [
        ListRow(id=f"{p.CUISINE_PREFIX}{c.slug}", title=c.name,
                description=f"{c.item_count} dishes")
        for c in cuisines[:MAX_CUISINE_ROWS]
    ]
    rows.append(ListRow(id=p.CHECK_AVAILABILITY,
                        title=p.CHECK_AVAILABILITY_LABEL,
                        description=p.CHECK_AVAILABILITY_DESC))

    await ctx.reply_list(p.CUISINE_PROMPT,
                         [ListSection(title="Our kitchens", rows=rows)],
                         button_text=p.CUISINE_LIST_BUTTON)
    ctx.goto(ConversationStep.CUISINE_MENU)


async def handle_cuisine_menu(ctx: FlowContext) -> None:
    choice = ctx.choice.lower()

    if choice == p.CHECK_AVAILABILITY:
        await ctx.request_location(p.ASK_AVAILABILITY_LOCATION)
        ctx.goto(ConversationStep.AWAIT_AVAILABILITY_LOCATION)
        return

    slug = _match_cuisine(choice, await menu_service.list_cuisines(ctx.session))
    if not slug:
        await ctx.reply_text(p.FALLBACK)
        await show_cuisines(ctx)
        return

    ctx.customer.cuisine_preference = slug
    ctx.put(cuisine=slug, category_page=0)
    await ctx.set_stage(LeadStage.CUISINE_SELECTED)
    await push_details(ctx.customer, cuisine=slug)
    await show_categories(ctx)


# --- step 6a: Check Availability ---------------------------------------------
async def handle_availability_location(ctx: FlowContext) -> None:
    """Optional serviceability check before browsing.

    On success the location is saved so step 9 can be skipped later, exactly
    as the spec describes.
    """
    postal_code = clean_postal_code(ctx.text)
    point = await kitchen_service.resolve_location(
        latitude=ctx.event.latitude,
        longitude=ctx.event.longitude,
        postal_code=postal_code,
    )
    if point is None:
        await ctx.reply_text(p.LOCATION_UNREADABLE)
        return

    check = await kitchen_service.check_service(ctx.session, point,
                                                postal_code=postal_code)
    if not check.is_serviceable:
        await ctx.reply_text(p.NOT_SERVICEABLE)
        await ctx.set_stage(LeadStage.NOT_SERVICEABLE)
        ctx.goto(ConversationStep.CUISINE_MENU)
        return

    _save_location(ctx, point, check.distance_km)
    distance = f" ({check.distance_display} away)" if check.distance_km else ""
    await ctx.reply_text(p.AVAILABILITY_OK.format(
        kitchen=check.kitchen_name, distance=distance))
    await show_cuisines(ctx)


# --- step 7: categories and dishes -------------------------------------------
async def show_categories(ctx: FlowContext, page: int = 0) -> None:
    cuisine_slug = ctx.get("cuisine") or ctx.customer.cuisine_preference
    if not cuisine_slug:
        await show_cuisines(ctx)
        return

    categories = await menu_service.list_categories(ctx.session, cuisine_slug)
    if not categories:
        await ctx.reply_text(p.MENU_EMPTY.format(cuisine=cuisine_slug.title()))
        await show_cuisines(ctx)
        return

    window, has_more = menu_service.paginate(categories, page)
    ctx.put(category_page=page)

    rows = [
        ListRow(id=f"{p.CATEGORY_PREFIX}{c.slug}", title=c.name,
                description=f"{c.item_count} dishes")
        for c in window
    ]
    rows.append(
        ListRow(id=f"{p.PAGE_PREFIX}{page + 1}", title=p.MORE_LABEL,
                description=p.MORE_DESC)
        if has_more
        else ListRow(id=p.BACK_TO_CUISINES, title=p.BACK_TO_CUISINES_LABEL)
    )

    await ctx.reply_list(
        p.CATEGORY_PROMPT.format(cuisine=cuisine_slug.title()),
        [ListSection(title="Categories", rows=rows)],
        button_text=p.CATEGORY_LIST_BUTTON,
    )
    ctx.goto(ConversationStep.BROWSING_CATEGORIES)


async def handle_categories(ctx: FlowContext) -> None:
    choice = ctx.choice

    if choice.startswith(p.PAGE_PREFIX):
        await show_categories(ctx, _page_from(choice))
        return
    if choice == p.BACK_TO_CUISINES:
        await show_cuisines(ctx)
        return
    if choice.startswith(p.CATEGORY_PREFIX):
        await show_items(ctx, choice[len(p.CATEGORY_PREFIX):], page=0)
        return

    # Typed text is treated as a dish search.
    if await _try_search(ctx):
        return

    await ctx.reply_text(p.FALLBACK)
    await show_categories(ctx, int(ctx.get("category_page") or 0))


async def show_items(ctx: FlowContext, category_slug: str, page: int = 0) -> None:
    cuisine_slug = ctx.get("cuisine") or ctx.customer.cuisine_preference
    items = await menu_service.list_items(ctx.session, cuisine_slug, category_slug)

    if not items:
        await ctx.reply_text(p.MENU_EMPTY.format(cuisine=cuisine_slug.title()))
        await show_categories(ctx)
        return

    window, has_more = menu_service.paginate(items, page)
    ctx.put(category=category_slug, item_page=page)

    rows = [
        ListRow(id=f"{p.ITEM_PREFIX}{item.retailer_id}", title=item.name,
                description=item.subtitle)
        for item in window
    ]
    rows.append(
        ListRow(id=f"{p.PAGE_PREFIX}{page + 1}", title=p.MORE_LABEL,
                description=p.MORE_DESC)
        if has_more
        else ListRow(id=p.BACK_TO_CATEGORIES, title=p.BACK_TO_CATEGORIES_LABEL)
    )

    await ctx.reply_list(
        p.ITEM_PROMPT.format(category=category_slug.replace("-", " ").title()),
        [ListSection(title="Dishes", rows=rows)],
        button_text=p.ITEM_LIST_BUTTON,
    )
    ctx.goto(ConversationStep.BROWSING_ITEMS)


async def handle_items(ctx: FlowContext) -> None:
    choice = ctx.choice

    if choice.startswith(p.PAGE_PREFIX):
        await show_items(ctx, ctx.get("category", ""), _page_from(choice))
        return
    if choice == p.BACK_TO_CATEGORIES:
        await show_categories(ctx, int(ctx.get("category_page") or 0))
        return
    if choice.startswith(p.ITEM_PREFIX):
        await _ask_quantity(ctx, choice[len(p.ITEM_PREFIX):])
        return

    if await _try_search(ctx):
        return

    await ctx.reply_text(p.FALLBACK)
    await show_items(ctx, ctx.get("category", ""), int(ctx.get("item_page") or 0))


# --- quantity and cart -------------------------------------------------------
async def _ask_quantity(ctx: FlowContext, retailer_id: str) -> None:
    found = await menu_service.get_by_retailer_ids(ctx.session, [retailer_id])
    item = found.get(retailer_id)
    if item is None:
        await ctx.reply_text(p.FALLBACK)
        await show_categories(ctx)
        return

    ctx.put(pending_item=retailer_id)
    await ctx.reply_text(p.ASK_QUANTITY.format(item=item.name,
                                                price=item.price_display))
    ctx.goto(ConversationStep.AWAIT_QUANTITY)


async def handle_quantity(ctx: FlowContext) -> None:
    retailer_id = ctx.get("pending_item")
    if not retailer_id:
        await show_categories(ctx)
        return

    quantity = _parse_quantity(ctx.choice)
    if quantity is None:
        await ctx.reply_text(p.QUANTITY_REASK.format(max=MAX_QUANTITY))
        return

    found = await menu_service.get_by_retailer_ids(ctx.session, [retailer_id])
    item = found.get(retailer_id)
    if item is None:
        await ctx.reply_text(p.FALLBACK)
        await show_categories(ctx)
        return

    _add_to_cart(ctx, retailer_id, item.name, item.price, quantity)
    ctx.drop("pending_item")
    await ctx.reply_text(p.ITEM_ADDED.format(quantity=quantity, item=item.name))
    await show_cart(ctx)


async def show_cart(ctx: FlowContext) -> None:
    """Cart review with Checkout / Add more / Clear (spec step 8)."""
    lines = ctx.get("cart") or []
    if not lines:
        await ctx.reply_text(p.CART_EMPTY)
        await show_categories(ctx)
        return

    await ctx.set_stage(LeadStage.CART_CREATED)
    await ctx.reply_buttons(
        f"{p.format_cart(lines)}\n\n{p.CART_PROMPT}",
        [
            Button(p.CART_CHECKOUT, p.BTN_CHECKOUT),
            Button(p.CART_ADD_MORE, p.BTN_ADD_MORE),
            Button(p.CART_CLEAR, p.BTN_CLEAR_CART),
        ],
    )
    ctx.goto(ConversationStep.CART_REVIEW)


async def handle_cart_review(ctx: FlowContext) -> None:
    from app.services.conversation.handlers import location

    choice = ctx.choice.lower()

    if choice == p.CART_CHECKOUT or choice.startswith("checkout"):
        await location.request_or_resume(ctx)
        return

    if choice == p.CART_ADD_MORE or choice.startswith("add"):
        await show_categories(ctx, int(ctx.get("category_page") or 0))
        return

    if choice == p.CART_CLEAR or choice.startswith("clear"):
        ctx.drop("cart")
        await ctx.reply_text(p.CART_CLEARED)
        await show_categories(ctx)
        return

    if await _try_search(ctx):
        return

    await ctx.reply_text(p.FALLBACK)
    await show_cart(ctx)


# --- helpers -----------------------------------------------------------------
def _add_to_cart(ctx: FlowContext, retailer_id: str, name: str,
                 price: Decimal, quantity: int) -> None:
    """Add a line, merging with an existing one for the same dish.

    Delegates to `services.cart` so the WhatsApp cart and the web ordering
    page are literally the same list, not two implementations that agree.
    """
    cart_service.add(ctx.conversation, retailer_id=retailer_id, name=name,
                     price=price, quantity=quantity)


def _parse_quantity(text: str) -> int | None:
    digits = "".join(ch for ch in (text or "") if ch.isdigit())
    if not digits:
        return None
    try:
        value = int(digits)
    except ValueError:
        return None
    return value if 1 <= value <= MAX_QUANTITY else None


def _page_from(choice: str) -> int:
    try:
        return int(choice[len(p.PAGE_PREFIX):])
    except ValueError:
        return 0


def _match_cuisine(choice: str, cuisines) -> str:
    """Accept the reply id, or the cuisine name typed out."""
    if choice.startswith(p.CUISINE_PREFIX):
        slug = choice[len(p.CUISINE_PREFIX):].strip()
        return slug if any(c.slug == slug for c in cuisines) else ""

    typed = choice.strip().lower()
    for cuisine in cuisines:
        if typed in (cuisine.slug, cuisine.name.lower()):
            return cuisine.slug
    return ""


async def _try_search(ctx: FlowContext) -> bool:
    """Treat typed text as a dish search. Returns True if it was handled."""
    term = ctx.text.strip()
    if len(term) < 3 or ctx.event.reply_id:
        return False

    results = await menu_service.search_items(
        ctx.session, term, cuisine_slug=ctx.get("cuisine")
    )
    if not results:
        await ctx.reply_text(p.SEARCH_NO_RESULTS.format(term=term))
        return False

    rows = [
        ListRow(id=f"{p.ITEM_PREFIX}{item.retailer_id}", title=item.name,
                description=item.subtitle)
        for item in results
    ]
    rows.append(ListRow(id=p.BACK_TO_CATEGORIES,
                        title=p.BACK_TO_CATEGORIES_LABEL))

    await ctx.reply_list(f'Dishes matching "{term}":',
                         [ListSection(title="Results", rows=rows)],
                         button_text=p.ITEM_LIST_BUTTON)
    ctx.goto(ConversationStep.BROWSING_ITEMS)
    return True


def _save_location(ctx: FlowContext, point, distance_km: float | None) -> None:
    ctx.customer.latitude = point.latitude
    ctx.customer.longitude = point.longitude
    if point.postal_code:
        ctx.customer.postal_code = point.postal_code
    if distance_km is not None:
        ctx.customer.distance_km = distance_km
    ctx.put(latitude=point.latitude, longitude=point.longitude,
            postal_code=point.postal_code, city=point.city, state=point.state,
            distance_km=distance_km)
