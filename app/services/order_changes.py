"""Undo an unpaid order so the customer can change it.

Behind the order_summary template's **Change menu** and **Update location**
buttons. An order that has not been paid is only a reservation: it holds a
delivery slot and a live Stripe link. Changing it means releasing both and
putting the dishes back in the cart, so the next link opens exactly where
the customer left off.

A paid order is never touched here - that is a refund, and a human's call.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import (Conversation, Customer, LeadStage, Order, OrderStage,
                           PaymentStatus)
from app.integrations.stripe_gw import checkout
from app.services import crm_sync, orders, slots

log = get_logger(__name__)


@dataclass(frozen=True)
class Reopened:
    """What happened to the customer's latest order."""
    order_number: str | None = None   # the order released, if there was one
    already_paid: bool = False        # nothing done: it is paid


async def reopen_latest(session: AsyncSession, customer: Customer,
                        conversation: Conversation, *, reason: str) -> Reopened:
    """Release the customer's unpaid order and restore its dishes to the cart.

    No pending order at all (already expired, or never placed) is fine: the
    cart is whatever it is and the caller sends a fresh link either way.
    """
    order = await orders.get_latest_for_customer(session, customer.id)
    if order is None:
        return Reopened()
    # Checked before the stage: payment moves an order past Pending Payment,
    # so a stage check first would treat a paid order as "nothing pending".
    if order.payment_status == PaymentStatus.PAID:
        return Reopened(order_number=order.order_number, already_paid=True)
    if order.stage != OrderStage.PENDING_PAYMENT:
        return Reopened()

    await _release(session, order, reason=reason)
    conversation.set(cart=as_cart(order.items or []))
    conversation.clear("order_id", "order_number", "slot_id", "slot_label",
                       "slot_starts_at", "slot_ends_at")
    await session.flush()
    # Back to a cart: the funnel says so, or the Lead would still read
    # Payment Link Sent for a link that no longer works.
    await crm_sync.advance_stage(customer, LeadStage.CART_CREATED)
    log.info("order_reopened", order_number=order.order_number, reason=reason)
    return Reopened(order_number=order.order_number)


def as_cart(items: list[dict]) -> list[dict]:
    """Order lines back into cart lines (the shape services/cart.py keeps)."""
    cart = []
    for line in items:
        retailer_id = str(line.get("retailer_id") or "").strip()
        try:
            quantity = int(line.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        if retailer_id and quantity > 0:
            cart.append({"retailer_id": retailer_id, "name": line.get("name") or "",
                         "quantity": quantity,
                         "unit_price": str(line.get("unit_price") or "0")})
    return cart


async def _release(session: AsyncSession, order: Order, *, reason: str) -> None:
    await slots.release_holds_for_order(session, order.id, reason=reason)
    orders.set_stage(order, OrderStage.CANCELLED)
    # Its Pay Now link is dead from here on. Left as "link sent", the reminder
    # and expiry jobs would still message the customer about it later.
    if order.payment_status == PaymentStatus.LINK_SENT:
        order.payment_status = PaymentStatus.EXPIRED
    # The old Pay Now link must stop working, or the customer could pay for
    # an order that no longer holds a slot. expire_session logs rather than
    # raises when Stripe says it is already expired.
    if order.stripe_session_id:
        try:
            await checkout.expire_session(order.stripe_session_id)
        except Exception:
            # Stripe unreachable. The old link stays payable until its own
            # 30-minute expiry, and a payment on it would still be accepted
            # by the webhook - so say so loudly for staff to watch.
            log.exception("stripe_expire_failed_old_link_still_payable",
                          order_number=order.order_number)
