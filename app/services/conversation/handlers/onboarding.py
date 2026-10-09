"""Entry, returning-customer check, name, email and main menu (steps 2-5)."""

from __future__ import annotations

from app.core.logging import get_logger
from app.db.models import ConversationStep, LeadStage
from app.integrations.gallabox import templates as tpl
from app.integrations.gallabox.messages import Button
from app.services.conversation import prompts as p
from app.services.conversation.context import FlowContext
from app.services.conversation.validators import clean_email, clean_name
from app.services import reply_triggers
from app.services.crm_sync import ensure_record, push_details

log = get_logger(__name__)


async def start(ctx: FlowContext) -> None:
    """Steps 2-3: greet by name if we know them, otherwise ask for it.

    The spec's returning-customer rule: if a Contact exists, greet by name and
    skip straight to the main menu.
    """
    await ensure_record(ctx.customer)

    if ctx.customer.has_details:
        await show_main_menu(ctx, returning=True)
        return

    await ctx.reply_text(p.WELCOME_ASK_NAME)
    ctx.goto(ConversationStep.AWAIT_NAME)


async def handle_name(ctx: FlowContext) -> None:
    """Step 3: capture the name, re-asking if it is empty or nonsense."""
    name = clean_name(ctx.text)
    if name is None:
        await ctx.reply_text(p.NAME_REASK)
        return

    ctx.customer.name = name
    await _ask_email(ctx, p.ASK_EMAIL.format(name=name))
    ctx.goto(ConversationStep.AWAIT_EMAIL)


async def _ask_email(ctx: FlowContext, question: str) -> None:
    """The email question, always with a way back to fix the name.

    The name is echoed in the question ("Thanks Asha!"), which is exactly when
    somebody notices it is wrong - so the button sits right there, and again on
    a re-ask, rather than only on the first try.
    """
    await ctx.reply_buttons(question, [Button(p.CHANGE_NAME, p.BTN_CHANGE_NAME)])


async def handle_email(ctx: FlowContext) -> None:
    """Step 4: capture the email, re-asking if the format is invalid."""
    # Checked on the tapped id, not the text: typing "change name" here is a
    # (bad) email address, and must be treated as one.
    if ctx.event.reply_id == p.CHANGE_NAME:
        await ctx.reply_text(p.ASK_NAME_AGAIN)
        ctx.goto(ConversationStep.AWAIT_NAME)
        return

    email = clean_email(ctx.text)
    if email is None:
        await _ask_email(ctx, p.EMAIL_REASK)
        return

    ctx.customer.email = email
    # Both details are now in hand (spec stage "Details Captured").
    await ctx.set_stage(LeadStage.DETAILS_CAPTURED)
    await push_details(ctx.customer, name=ctx.customer.name, email=email)
    await show_main_menu(ctx)


async def show_main_menu(ctx: FlowContext, *, returning: bool = False) -> None:
    """Step 5: the greeting, with the two ways to order.

    Order Now opens the shop's website; Continue on WhatsApp carries on here.
    One WhatsApp message cannot hold a link button and a reply button, and a
    template that could was filed by Meta as Marketing - which Meta does not
    deliver to US numbers. So it is two ordinary messages, sent back to back:
    the greeting with the Order Now link button, then the reply button.

    Stale ids from older buttons (`menu:order`, `menu:talk`) are still
    honoured by `handle_main_menu`, so nobody who tapped one is left without
    an answer.

    The trigger keyword that started the chat may ask for only one of them
    (Bot replies page, services/reply_triggers.REPLIES): the website alone,
    or WhatsApp alone, which is then the greeting and the button together.
    """
    name = ctx.customer.greeting_name
    greeting = (p.WELCOME_BACK if returning else p.WELCOME_READY).format(name=name)
    reply = reply_triggers.welcome_reply(ctx.conversation.get("welcome_reply"))
    continue_button = [Button(tpl.CONTINUE_ON_WHATSAPP, tpl.CONTINUE_ON_WHATSAPP)]

    if reply == reply_triggers.WHATSAPP:
        await ctx.reply_buttons(f"{greeting}\n\n{p.CONTINUE_ONLY_PROMPT}", continue_button)
    else:
        await ctx.reply_cta_url(greeting, url=tpl.SHERO_WEBSITE, display_text=p.BTN_ORDER_NOW)
        if reply == reply_triggers.BOTH:
            await ctx.reply_buttons(p.CONTINUE_PROMPT, continue_button)
    ctx.goto(ConversationStep.MAIN_MENU)


async def handle_main_menu(ctx: FlowContext) -> None:
    """Route the main-menu choice."""
    # Imported here rather than at module level to avoid an import cycle
    # between the onboarding and menu handlers, which call into each other.
    from app.services.conversation.handlers import menu

    choice = ctx.choice.lower()
    if choice == p.MENU_TALK or choice in p.AGENT_KEYWORDS:
        await hand_over(ctx)
        return

    if choice == p.MENU_ORDER or "order" in choice or choice in p.CONTINUE_KEYWORDS:
        await menu.show_cuisines(ctx)
        return

    await ctx.reply_text(p.FALLBACK)
    await show_main_menu(ctx)


async def hand_over(ctx: FlowContext) -> None:
    """'Talk to Us' - stop the bot and pass the thread to a Gallabox agent."""
    await ctx.reply_text(p.HANDOVER)
    await ctx.handover(note=f"Requested at step {ctx.step}")
    ctx.goto(ConversationStep.HANDED_OVER)
    log.info("handed_over_to_agent", customer_id=str(ctx.customer.id))
