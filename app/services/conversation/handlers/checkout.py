"""Order summary, confirmation and payment link (steps 14-15)."""

from __future__ import annotations

from datetime import datetime

from app.core.logging import get_logger
from app.core.exceptions import PaymentError
from app.db.models import ConversationStep, Order, OrderStage, Outlet, PaymentStatus
from app.integrations.gallabox.messages import Button
from app.integrations.uber.direct import build_address
from app.services import kitchen as kitchen_service
from app.services import orders as order_service
from app.services import order_changes, payments, promotions, slots
from app.services.conversation import prompts as p
from app.services.conversation.context import FlowContext
from app.services.pricing import format_summary, price_cart

log = get_logger(__name__)


async def build_and_show_summary(ctx: FlowContext) -> None:
    """Step 14: price everything, create the draft order, show the summary."""
    outlet = await _outlet(ctx)
    if outlet is None:
        await ctx.reply_text(p.GENERIC_ERROR)
        return

    cart = ctx.get("cart") or []
    slot_starts_at = _parse_dt(ctx.get("slot_starts_at"))
    # A coupon applied on the ordering page counts here too: the code is kept
    # on the conversation, which the page and the chat share.
    promotion, coupon_notice = await promotions.stored_promotion(
        ctx.session, ctx.conversation, customer=ctx.customer, outlet=outlet, lock=True)

    priced = await price_cart(
        ctx.session,
        cart,
        outlet=outlet,
        dropoff_latitude=ctx.customer.latitude,
        dropoff_longitude=ctx.customer.longitude,
        dropoff_address=_dropoff_address(ctx),
        slot_starts_at=slot_starts_at,
        promotion=promotion,
        customer_id=ctx.customer.id,
        loyalty_points=ctx.get("loyalty_points") or 0,
    )

    # Without a delivery quote we would have to guess the charge, so stop here
    # rather than show the customer a total we cannot stand behind.
    if priced.delivery_quote_failed:
        await ctx.reply_text(p.QUOTE_FAILED)
        log.warning("summary_blocked_no_quote", outlet=outlet.code)
        return

    order = await order_service.create_draft_order(
        ctx.session,
        customer=ctx.customer,
        outlet=outlet,
        priced=priced,
        slot_id=ctx.get("slot_id"),
        slot_starts_at=slot_starts_at,
        slot_ends_at=_parse_dt(ctx.get("slot_ends_at")),
        slot_label=ctx.get("slot_label"),
        distance_km=ctx.get("distance_km"),
    )

    # Hold the slot now: the spec keeps it reserved while payment is pending.
    if ctx.get("slot_id"):
        try:
            await slots.hold_slot(ctx.session, slot_id=ctx.get("slot_id"), order_id=order.id)
        except Exception:
            from app.services import loyalty
            await loyalty.release(ctx.session, order, reason="Delivery slot could not be reserved")
            order_service.set_stage(order, OrderStage.CANCELLED)
            await ctx.session.flush()
            raise
    await order_changes.release_other_promo_holds(ctx.session, order)

    ctx.put(order_id=str(order.id), order_number=order.order_number)
    ctx.conversation.clear("loyalty_points")

    summary = format_summary(priced, outlet_name=outlet.name,
                             slot_label=ctx.get("slot_label") or "your chosen slot")
    # Say why a coupon is not on the total, rather than silently leaving it off.
    note = coupon_notice or priced.promo_problem
    if note:
        summary = f"{summary}\n\n{note}"
    await ctx.reply_buttons(
        f"{p.SUMMARY_PROMPT}\n\n{summary}",
        [
            Button(p.SUMMARY_CONFIRM, p.BTN_CONFIRM),
            Button(p.SUMMARY_EDIT, p.BTN_EDIT),
            Button(p.SUMMARY_CANCEL, p.BTN_CANCEL),
        ],
        # A code can be typed right here (handlers/coupons.py).
        footer=p.SUMMARY_COUPON_HINT if promotion is None else None,
    )
    ctx.goto(ConversationStep.AWAIT_SUMMARY_CONFIRM)


async def reprice_summary(ctx: FlowContext) -> None:
    """Show the summary again after a coupon was applied or removed in chat.

    The draft is cancelled and priced afresh on the same slot: its hold is
    released only for the new draft to take it back, and any points it used
    are asked for again.
    """
    from app.services.conversation.handlers import coupons

    order = await _draft_order(ctx)
    points = order.loyalty_points if order is not None else 0
    if points and promotions.stored_code(ctx.conversation) and not await coupons.stacks_with_points(ctx):
        # The coupon was asked for last, so it wins; the customer is told.
        await ctx.reply_text(p.POINTS_NOT_WITH_COUPON.format(points=points))
        points = 0
    if points and not ctx.get("loyalty_points"):
        ctx.put(loyalty_points=points)
    await _abandon_draft(ctx, order, reason="coupon_changed", keep_slot=True)
    await build_and_show_summary(ctx)


async def handle_summary_choice(ctx: FlowContext) -> None:
    """Step 14: Confirm / Edit / Cancel."""
    from app.services.conversation.handlers import menu

    choice = ctx.choice.lower()
    order = await _draft_order(ctx)

    if choice == p.SUMMARY_CONFIRM or choice.startswith("confirm"):
        await _send_payment(ctx, order)
        return

    if choice == p.SUMMARY_EDIT or choice.startswith("edit"):
        # Spec: "Edit -> back to step 7". The draft is abandoned and its slot
        # released so a re-priced order starts clean.
        # Spec: "Edit -> back to the menu". The draft is abandoned and its
        # slot released, but the cart is kept so they only adjust it.
        await _abandon_draft(ctx, order, reason="edited")
        if ctx.get("cart"):
            await menu.show_cart(ctx)
        elif ctx.get("cuisine") or ctx.customer.cuisine_preference:
            await menu.show_categories(ctx)
        else:
            await menu.show_cuisines(ctx)
        return

    if choice == p.SUMMARY_CANCEL or choice.startswith("cancel"):
        await _abandon_draft(ctx, order, reason="cancelled")
        await ctx.reply_text(p.ORDER_CANCELLED)
        ctx.goto(ConversationStep.COMPLETED)
        return

    await ctx.reply_text(p.FALLBACK)


async def _send_payment(ctx: FlowContext, order: Order | None) -> None:
    """Step 15: create the Stripe link and send the payment template."""
    if order is None:
        log.error("confirm_without_draft_order", customer_id=str(ctx.customer.id))
        await ctx.reply_text(p.GENERIC_ERROR)
        return

    await ctx.reply_text(p.PAYMENT_SENDING)
    try:
        await payments.create_payment_link(ctx.session, order, ctx.customer)
    except PaymentError:
        # Creation failed before a payable Stripe session existed. Do not
        # strand the points on a draft that has no expiry job to release it.
        if not order.stripe_session_id:
            await _abandon_draft(ctx, order, reason="payment_link_failed")
        raise
    ctx.goto(ConversationStep.AWAIT_PAYMENT)


async def _abandon_draft(ctx: FlowContext, order: Order | None, *,
                         reason: str, keep_slot: bool = False) -> None:
    """Release an unpaid draft order and its slot hold. `keep_slot` leaves
    the chosen slot on the conversation, for a draft about to be re-priced."""
    if order is None:
        return
    if order.payment_status == PaymentStatus.PAID:
        log.warning("abandon_skipped_paid_order", order_number=order.order_number)
        return

    from app.services import loyalty
    await loyalty.release(ctx.session, order, reason=reason)
    await slots.release_holds_for_order(ctx.session, order.id, reason=reason)
    # set_stage stamps cancelled_at as part of the transition.
    order_service.set_stage(order, OrderStage.CANCELLED)
    ctx.drop("order_id", "order_number")
    if not keep_slot:
        ctx.drop("slot_id", "slot_label", "slot_starts_at", "slot_ends_at")
    await ctx.session.flush()
    log.info("draft_order_abandoned", order_number=order.order_number, reason=reason)


# --- helpers -----------------------------------------------------------------
async def _outlet(ctx: FlowContext) -> Outlet | None:
    """The kitchen this order is cooked at: the nearest one picked for the
    customer's address at the location step."""
    return await kitchen_service.kitchen_for(ctx.session, ctx.customer,
                                             ctx.get("outlet_id"))


async def _draft_order(ctx: FlowContext) -> Order | None:
    order_id = ctx.get("order_id")
    if not order_id:
        return None
    return await order_service.get_order(ctx.session, order_id)


def _dropoff_address(ctx: FlowContext) -> dict:
    """Customer address in the shape Uber Direct expects."""
    return build_address(
        street=ctx.customer.address_line1 or "",
        city=ctx.get("city") or "",
        state=ctx.get("state") or "",
        zip_code=ctx.customer.postal_code or ctx.get("postal_code") or "",
    )


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
