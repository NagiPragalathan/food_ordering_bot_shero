"""The conversation dispatcher.

One entry point, `handle_event`, which every inbound WhatsApp message goes
through:

    dedupe -> load customer/conversation -> global intents -> step handler

Routing is a table rather than a chain of ifs, so the flow in
`docs/conversation-flow.md` can be read straight off `STEP_HANDLERS`.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BotFlowError, IntegrationError
from app.core.logging import get_logger
from app.db.models import ConversationStep, InboundMessage
from app.schemas.inbound import InboundEvent, InboundKind
from app.services.conversation import prompts as p
from app.services.conversation.context import FlowContext
from app.services.conversation.handlers import (
    checkout,
    delivery,
    feedback,
    location,
    menu,
    onboarding,
)
from app.services.customers import get_or_create_conversation, get_or_create_customer

log = get_logger(__name__)


# Steps whose answer is free text, where a word like "hi" or "help" is data,
# not a command. Global keyword intents are suppressed here so a customer
# living on "Help Street" can still give their address.
FREE_TEXT_STEPS = {
    ConversationStep.AWAIT_NAME,
    ConversationStep.AWAIT_EMAIL,
    ConversationStep.AWAIT_ADDRESS,
    ConversationStep.AWAIT_APARTMENT,
    ConversationStep.AWAIT_INSTRUCTIONS,
    ConversationStep.AWAIT_CONTACT_NUMBER,
    ConversationStep.AWAIT_LOCATION,
    ConversationStep.AWAIT_AVAILABILITY_LOCATION,
    # A bare number, where "no" or "start" would be a mis-read.
    ConversationStep.AWAIT_QUANTITY,
}

STEP_HANDLERS = {
    ConversationStep.START: onboarding.start,
    ConversationStep.AWAIT_NAME: onboarding.handle_name,
    ConversationStep.AWAIT_EMAIL: onboarding.handle_email,
    ConversationStep.MAIN_MENU: onboarding.handle_main_menu,
    ConversationStep.CUISINE_MENU: menu.handle_cuisine_menu,
    ConversationStep.AWAIT_AVAILABILITY_LOCATION: menu.handle_availability_location,
    ConversationStep.BROWSING_CATEGORIES: menu.handle_categories,
    ConversationStep.BROWSING_ITEMS: menu.handle_items,
    ConversationStep.AWAIT_QUANTITY: menu.handle_quantity,
    ConversationStep.CART_REVIEW: menu.handle_cart_review,
    ConversationStep.AWAIT_LOCATION: location.handle_location,
    ConversationStep.AWAIT_ADDRESS: delivery.handle_address,
    ConversationStep.AWAIT_APARTMENT: delivery.handle_apartment,
    ConversationStep.AWAIT_INSTRUCTIONS: delivery.handle_instructions,
    ConversationStep.AWAIT_CONTACT_NUMBER: delivery.handle_contact_number,
    ConversationStep.AWAIT_SLOT_CHOICE: delivery.handle_slot_choice,
    ConversationStep.AWAIT_SUMMARY_CONFIRM: checkout.handle_summary_choice,
    ConversationStep.AWAIT_FEEDBACK: feedback.handle_feedback,
}


async def handle_event(session: AsyncSession, event: InboundEvent) -> bool:
    """Process one inbound message. Returns False if it was ignored."""
    if not event.is_actionable:
        log.info("inbound_ignored", reason="not actionable",
                 kind=str(event.kind), raw_keys=list(event.raw)[:8])
        return False

    if not await _claim_message(session, event):
        return False

    customer, created = await get_or_create_customer(
        session,
        event.whatsapp_number,
        ad_id=event.ad_id or None,
        campaign_id=event.campaign_id or None,
        referral_payload=event.referral,
    )
    # A name supplied by WhatsApp itself saves asking, but never overwrites
    # a name the customer typed.
    if created and event.contact_name and not customer.name:
        customer.name = event.contact_name

    conversation = await get_or_create_conversation(session, customer)
    ctx = FlowContext(session=session, customer=customer,
                      conversation=conversation, event=event)

    try:
        await _dispatch(ctx)
    except BotFlowError as exc:
        # Expected flow failures carry a message written for the customer.
        log.info("flow_error", step=ctx.step, error=str(exc))
        await _safe_reply(ctx, exc.customer_message)
    except IntegrationError as exc:
        log.error("integration_error", step=ctx.step, service=exc.service,
                  error=str(exc))
        await _safe_reply(ctx, p.GENERIC_ERROR)
    except Exception:
        # Unexpected: log with a stack trace, but still say something rather
        # than leaving the customer staring at silence.
        log.exception("unhandled_flow_error", step=ctx.step)
        await _safe_reply(ctx, p.GENERIC_ERROR)

    return True


async def _dispatch(ctx: FlowContext) -> None:
    """Route one event to the right handler."""
    # A native catalogue cart can still arrive if the Meta catalogue is ever
    # wired up alongside this menu; treat it as a checkout request.
    if ctx.event.kind is InboundKind.CART and ctx.event.cart_lines:
        await _adopt_native_cart(ctx)
        return

    if await _handle_global_intent(ctx):
        return

    step = _current_step(ctx)

    # A conversation already handed to a human stays with the human.
    if step is ConversationStep.HANDED_OVER:
        log.info("message_while_handed_over", customer_id=str(ctx.customer.id))
        return

    # A finished conversation restarts on the next message.
    if step in (ConversationStep.COMPLETED, ConversationStep.AWAIT_PAYMENT):
        await _resume(ctx, step)
        return

    handler = STEP_HANDLERS.get(step)
    if handler is None:
        log.warning("no_handler_for_step", step=str(step))
        await onboarding.start(ctx)
        return

    await handler(ctx)


async def _handle_global_intent(ctx: FlowContext) -> bool:
    """Keywords that work from anywhere. Returns True if one fired."""
    if _current_step(ctx) in FREE_TEXT_STEPS:
        return False

    text = ctx.text.lower().strip()
    if not text:
        return False

    if text in p.AGENT_KEYWORDS:
        await onboarding.hand_over(ctx)
        return True

    if text in p.RESTART_KEYWORDS:
        if ctx.customer.has_details:
            await onboarding.show_main_menu(ctx)
        else:
            await onboarding.start(ctx)
        return True

    return False


async def _resume(ctx: FlowContext, step: ConversationStep) -> None:
    """Handle a message that arrives after the flow has parked.

    While waiting on payment the customer may still want a human, or may be
    ready to start a new order; either way the bot should respond.
    """
    if step is ConversationStep.AWAIT_PAYMENT:
        # Nudge rather than restart: their payment link is still live.
        await ctx.reply_text(
            "Your payment link is still open - tap Pay Now in the message above "
            "to finish your order, or type 'agent' if you need help."
        )
        return

    await onboarding.start(ctx)


def _current_step(ctx: FlowContext) -> ConversationStep:
    try:
        return ConversationStep(ctx.conversation.step)
    except ValueError:
        log.warning("unknown_step_value", step=ctx.conversation.step)
        return ConversationStep.START


async def _claim_message(session: AsyncSession, event: InboundEvent) -> bool:
    """Record the provider message id, or report a replay.

    Gallabox retries on any non-2xx, so without this a transient failure
    halfway through a step would re-run the earlier half on every retry.
    """
    if not event.message_id:
        # Nothing to dedupe on: process it, but say so in the logs.
        log.info("inbound_without_message_id", number=event.whatsapp_number[-4:])
        return True

    record = InboundMessage(
        provider_message_id=event.message_id,
        whatsapp_number=event.whatsapp_number,
        payload=event.raw,
    )
    session.add(record)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        log.info("inbound_duplicate_ignored", message_id=event.message_id)
        return False
    return True


async def _safe_reply(ctx: FlowContext, body: str) -> None:
    """Send an error message, swallowing a further failure.

    If WhatsApp itself is the thing that is broken, there is nothing useful
    left to do but log it.
    """
    try:
        await ctx.reply_text(body)
    except Exception as exc:  # noqa: BLE001
        log.error("error_reply_failed", error=str(exc))


async def _adopt_native_cart(ctx: FlowContext) -> None:
    """Accept a cart sent from a Meta catalogue message.

    The bot normally builds the cart itself, but if the client later enables
    the native WhatsApp catalogue the incoming lines are merged in and the
    customer goes straight to the cart review.
    """
    from app.services import menu as menu_service

    retailer_ids = [line.retailer_id for line in ctx.event.cart_lines]
    known = await menu_service.get_by_retailer_ids(ctx.session, retailer_ids)

    lines = []
    for line in ctx.event.cart_lines:
        item = known.get(line.retailer_id)
        if item is None:
            continue
        lines.append({
            "retailer_id": item.retailer_id,
            "name": item.name,
            "quantity": max(int(line.quantity or 1), 1),
            "unit_price": str(item.price),
        })

    if not lines:
        log.info("native_cart_had_no_known_items")
        await menu.show_cuisines(ctx)
        return

    ctx.put(cart=lines)
    await menu.show_cart(ctx)
