"""Entry, returning-customer check, name, email and main menu (steps 2-5)."""

from __future__ import annotations

from app.core.logging import get_logger
from app.db.models import ConversationStep, LeadStage
from app.integrations.gallabox.messages import Button
from app.services.conversation import prompts as p
from app.services.conversation.context import FlowContext
from app.services.conversation.validators import clean_email, clean_name
from app.services.crm_sync import ensure_lead, push_details

log = get_logger(__name__)


async def start(ctx: FlowContext) -> None:
    """Steps 2-3: greet by name if we know them, otherwise ask for it.

    The spec's returning-customer rule: if a Contact exists, greet by name and
    skip straight to the main menu.
    """
    await ensure_lead(ctx.customer)

    if ctx.customer.has_details:
        await ctx.reply_text(p.WELCOME_BACK.format(name=ctx.customer.name))
        await show_main_menu(ctx)
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
    await ctx.reply_text(p.ASK_EMAIL.format(name=name))
    ctx.goto(ConversationStep.AWAIT_EMAIL)


async def handle_email(ctx: FlowContext) -> None:
    """Step 4: capture the email, re-asking if the format is invalid."""
    email = clean_email(ctx.text)
    if email is None:
        await ctx.reply_text(p.EMAIL_REASK)
        return

    ctx.customer.email = email
    # Both details are now in hand (spec stage "Details Captured").
    await ctx.set_stage(LeadStage.DETAILS_CAPTURED)
    await push_details(ctx.customer, name=ctx.customer.name, email=email)
    await show_main_menu(ctx)


async def show_main_menu(ctx: FlowContext) -> None:
    """Step 5: Order Online / Talk to Us."""
    await ctx.reply_buttons(
        p.MAIN_MENU,
        [
            Button(p.MENU_ORDER, p.BTN_ORDER_ONLINE),
            Button(p.MENU_TALK, p.BTN_TALK_TO_US),
        ],
    )
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

    if choice == p.MENU_ORDER or "order" in choice:
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
