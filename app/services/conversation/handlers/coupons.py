"""Coupon codes typed in the chat: "offers", "apply CODE", "remove coupon".

The ordering page has its own coupon box (api/routes/order_web.py); this is
the same for a customer who orders inside WhatsApp. Both keep the code in one
place on the conversation (services/promotions.py), so a code applied on one
shows on the other, and every rule (dates, limits, first order, kitchen,
minimum order) is checked the same way.

A code applied before the summary is checked now and used when the order is
priced. One applied at the summary re-prices it straight away on the same
slot. Once a payment link is out its total is fixed, so the customer is told
to type "change menu" first, as for loyalty points.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.db.models import ConversationStep, Order, OrderStage, PaymentStatus
from app.services import cart, promotions
from app.services import kitchen as kitchen_service
from app.services import orders as order_service
from app.services.conversation import prompts as p
from app.services.conversation.context import FlowContext
from app.services.conversation.handlers import checkout

log = get_logger(__name__)

OFFERS_KEYWORDS = {"offers", "offer", "coupons", "coupon", "coupon code", "promo",
                   "promo code", "promos", "deals", "discount", "discounts"}
REMOVE_KEYWORDS = {"remove coupon", "remove code", "remove promo", "no coupon",
                   "coupon remove", "coupon off"}
# Longest first, so "coupon code X" is not read as the code "CODEX".
APPLY_PREFIXES = ("coupon code ", "promo code ", "apply ", "coupon ", "promo ", "code ")
# One message lists at most this many; the rest are on the ordering page.
MAX_LISTED = 5

HOW_TO_APPLY = "Type *apply* and the code, for example *apply {code}*."
LINK_OUT = ("Your payment link for {number} is already out, so its total cannot "
            "change. Type *change menu* to edit that order, then {action} the code.")


async def handle(ctx: FlowContext, text: str) -> bool:
    """`text` is the message, lower-cased. True if it was a coupon command."""
    if text in REMOVE_KEYWORDS:
        await remove(ctx)
        return True
    if text in OFFERS_KEYWORDS:
        await list_offers(ctx)
        return True
    # "apply X" is always a coupon; a code on its own (a reply to an offer)
    # only when it exists, so any other word carries on to the step as before.
    code, prefixed = _typed_code(text)
    if prefixed or await _exists(ctx.session, code):
        await apply(ctx, code)
        return True
    return False


async def is_command(session, message: str) -> bool:
    """Whether a message is for coupons, for services/reply_triggers: then it
    is answered even from a customer who is not part-way through an order.

    Stricter than `handle`: "apply ..." counts only with a real code, so
    "apply for a job" stays with the team when the bot answers keywords only.
    """
    text = " ".join((message or "").lower().split())
    if text in OFFERS_KEYWORDS or text in REMOVE_KEYWORDS:
        return True
    return await _exists(session, _typed_code(text)[0])


async def list_offers(ctx: FlowContext) -> None:
    lines = cart.read(ctx.conversation)
    offers = await promotions.offers_for(
        ctx.session, ctx.customer, outlet=await _kitchen(ctx),
        dish_total=cart.subtotal(lines) if lines else None)
    applied = promotions.stored_code(ctx.conversation)

    if offers:
        rows = []
        for offer in offers[:MAX_LISTED]:
            row = f"*{offer['code']}* - {offer['headline']}"
            if offer["terms"]:
                row += f" ({', '.join(offer['terms'])})"
            if offer["hint"]:
                row += f"\n   {offer['hint']}"
            rows.append(row)
        body = ("Offers you can use:\n\n" + "\n".join(rows) + "\n\n"
                + HOW_TO_APPLY.format(code=offers[0]["code"]))
    else:
        body = "There are no open offers right now. If you have a code, type *apply* and the code."
    if applied:
        body += f"\n\nApplied now: *{applied}*. Type *remove coupon* to take it off."
    await ctx.reply_text(body)


async def apply(ctx: FlowContext, raw: str) -> None:
    code = promotions.normalize_code(raw)
    open_link = await _open_payment_link(ctx)
    if open_link is not None:
        await ctx.reply_text(LINK_OUT.format(number=open_link.order_number, action="apply"))
        return

    try:
        promotion = await promotions.resolve(ctx.session, code, customer=ctx.customer,
                                             outlet=await _kitchen(ctx))
    except promotions.PromoError as exc:
        log.info("chat_coupon_rejected", customer_id=str(ctx.customer.id), code=code,
                 reason=str(exc))
        await ctx.reply_text(f"{exc}\n\nType *offers* to see the codes you can use.")
        return

    promotions.remember(ctx.conversation, promotion.code)
    log.info("chat_coupon_applied", customer_id=str(ctx.customer.id), code=promotion.code)
    applied = f"*{promotion.code}* applied: {promotions.headline(promotion)}."

    if ctx.step == str(ConversationStep.AWAIT_SUMMARY_CONFIRM):
        await ctx.reply_text(f"{applied} Here is your updated total.")
        await checkout.reprice_summary(ctx)
        return

    lines = cart.read(ctx.conversation)
    problem = (promotions.amount_problem(promotion, dish_total=cart.subtotal(lines))
               if lines else None)
    reply = f"{applied} {problem or 'You will see the saving on your order summary.'}"
    points = ctx.get("loyalty_points") or 0
    if points and not await stacks_with_points(ctx):
        ctx.drop("loyalty_points")
        reply += "\n\n" + p.POINTS_NOT_WITH_COUPON.format(points=points)
    await ctx.reply_text(reply)


async def remove(ctx: FlowContext) -> None:
    code = promotions.stored_code(ctx.conversation)
    if not code:
        await ctx.reply_text("You have no coupon applied. Type *offers* to see the codes you can use.")
        return
    open_link = await _open_payment_link(ctx)
    if open_link is not None:
        await ctx.reply_text(LINK_OUT.format(number=open_link.order_number, action="remove"))
        return

    promotions.forget(ctx.conversation)
    log.info("chat_coupon_removed", customer_id=str(ctx.customer.id), code=code)
    if ctx.step == str(ConversationStep.AWAIT_SUMMARY_CONFIRM):
        await ctx.reply_text(f"*{code}* removed. Here is your updated total.")
        await checkout.reprice_summary(ctx)
        return
    await ctx.reply_text(f"*{code}* removed.")


async def stacks_with_points(ctx: FlowContext) -> bool:
    """Whether the loyalty rules let points and a coupon share an order."""
    from app.services import loyalty
    return (await loyalty.policy(ctx.session)).stack_with_coupons


# --- helpers -----------------------------------------------------------------
def _typed_code(text: str) -> tuple[str, bool]:
    """The code in "apply X" and the like, and whether a prefix said so."""
    for prefix in APPLY_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):], True
    return text, False


async def _exists(session, code: str) -> bool:
    return (promotions.CODE_PATTERN.match(promotions.normalize_code(code)) is not None
            and await promotions.find_by_code(session, code) is not None)


async def _kitchen(ctx: FlowContext):
    return await kitchen_service.kitchen_for(ctx.session, ctx.customer, ctx.get("outlet_id"))


async def _open_payment_link(ctx: FlowContext) -> Order | None:
    """The chat's order, if its payment link is out and still payable."""
    order_id = ctx.get("order_id")
    if not order_id:
        return None
    order = await order_service.get_order(ctx.session, order_id)
    if (order is not None and order.payment_status == PaymentStatus.LINK_SENT
            and order.stage == OrderStage.PENDING_PAYMENT):
        return order
    return None
