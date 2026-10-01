"""Scheduled background jobs.

Five things happen on a clock rather than in response to a message:

  * the 15-minute unpaid payment reminder            (spec step 15)
  * the 30-minute payment expiry and slot release    (spec step 15)
  * the post-delivery feedback request               (spec step 19)
  * topping up delivery slots for the days ahead     (spec step 13)
  * picking up settings saved on another instance

Each job opens its own session, handles one order at a time and never lets a
single failure abort the batch - one broken order must not stop the other
twenty from being processed.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.models import (
    Conversation,
    ConversationStep,
    Customer,
    Outlet,
)
from app.db.session import session_scope
from app.integrations.gallabox import templates as tpl
from app.integrations.gallabox.client import gallabox
from app.services import orders as order_service
from app.services import dispatch, kitchen_alerts, payments, slots

log = get_logger(__name__)


async def send_payment_reminders() -> int:
    """Nudge customers who have not paid after 15 minutes."""
    sent = 0
    async with session_scope() as session:
        due = await order_service.find_orders_for_reminder(session)
        for order in due:
            customer = await session.get(Customer, order.customer_id)
            if customer is None:
                continue
            try:
                if await payments.send_reminder(session, order, customer):
                    sent += 1
            except Exception as exc:  # noqa: BLE001 - keep the batch going
                log.error("reminder_job_failed", order_number=order.order_number,
                          error=str(exc))
    if sent:
        log.info("payment_reminders_sent", count=sent)
    return sent


async def expire_payment_links() -> int:
    """Expire lapsed links and release their slots.

    Stripe also emits `checkout.session.expired`; this is the safety net for a
    webhook that never arrives. Both paths call the same idempotent handler.
    """
    expired = 0
    async with session_scope() as session:
        due = await order_service.find_expired_orders(session)
        for order in due:
            customer = await session.get(Customer, order.customer_id)
            if customer is None:
                continue
            try:
                if await payments.handle_payment_expired(session, order, customer):
                    expired += 1
            except Exception as exc:  # noqa: BLE001
                log.error("expiry_job_failed", order_number=order.order_number,
                          error=str(exc))
    if expired:
        log.info("payment_links_expired", count=expired)
    return expired


async def send_feedback_requests() -> int:
    """Ask for a rating 30 minutes after delivery (spec step 19)."""
    sent = 0
    async with session_scope() as session:
        due = await order_service.find_orders_for_feedback(session)
        for order in due:
            customer = await session.get(Customer, order.customer_id)
            if customer is None:
                continue
            try:
                await gallabox.send_template(
                    customer.whatsapp_number, tpl.FEEDBACK_REQUEST,
                    order.order_number,
                )
                order.feedback_requested_at = datetime.now(timezone.utc)
                await _park_for_feedback(session, customer)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                log.error("feedback_job_failed", order_number=order.order_number,
                          error=str(exc))
    if sent:
        log.info("feedback_requests_sent", count=sent)
    return sent


async def dispatch_couriers() -> int:
    """Book the Uber courier for orders whose slot starts within
    UBER_DISPATCH_HOURS_BEFORE (services/dispatch.py)."""
    booked = 0
    async with session_scope() as session:
        for order in await dispatch.due_orders(session):
            try:
                if await dispatch.dispatch(session, order):
                    booked += 1
            except Exception as exc:  # noqa: BLE001 - one bad order must not stop the rest
                log.error("dispatch_job_failed", order_number=order.order_number,
                          error=str(exc))
    if booked:
        log.info("couriers_booked", count=booked)
    return booked


async def send_kitchen_alerts() -> int:
    """Tell each kitchen about its orders on the delivery day
    (services/kitchen_alerts.py)."""
    sent = 0
    async with session_scope() as session:
        for order in await kitchen_alerts.due_alerts(session):
            try:
                if await kitchen_alerts.send_alert(session, order):
                    sent += 1
            except Exception as exc:  # noqa: BLE001 - one bad order must not stop the rest
                log.error("kitchen_alert_job_failed", order_number=order.order_number,
                          error=str(exc))
    if sent:
        log.info("kitchen_alerts_sent", count=sent)
    return sent


async def generate_upcoming_slots() -> int:
    """Keep every active outlet stocked with bookable slots."""
    created = 0
    async with session_scope() as session:
        result = await session.execute(
            select(Outlet).where(Outlet.is_active.is_(True))
        )
        for outlet in result.scalars():
            try:
                created += await slots.ensure_slots(session, outlet)
            except Exception as exc:  # noqa: BLE001
                log.error("slot_generation_failed", outlet=outlet.code,
                          error=str(exc))
    if created:
        log.info("slots_generated_scheduled", count=created)
    return created


async def refresh_settings() -> int:
    """Pick up settings saved through the admin dashboard elsewhere.

    The Settings page applies changes to its own instance immediately. This is
    what carries them to the others, so a rotated key does not need a restart
    across the fleet.
    """
    from app.services.settings_store import apply_overrides

    async with session_scope() as session:
        try:
            return await apply_overrides(session)
        except Exception as exc:  # noqa: BLE001 - never kill the scheduler
            log.error("settings_refresh_failed", error=str(exc))
            return 0


async def _park_for_feedback(session, customer: Customer) -> None:
    """Point the conversation at the feedback step so the reply is understood.

    Skipped if the customer is mid-order or with an agent - a rating request
    must not hijack a live conversation.
    """
    result = await session.execute(
        select(Conversation).where(Conversation.customer_id == customer.id)
    )
    conversation = result.scalar_one_or_none()
    if conversation is None:
        return

    parked = {ConversationStep.COMPLETED, ConversationStep.START,
              ConversationStep.MAIN_MENU}
    if conversation.step in {str(s) for s in parked}:
        conversation.previous_step = conversation.step
        conversation.step = str(ConversationStep.AWAIT_FEEDBACK)


# Exposed for the scheduler and for manual invocation from a shell.
ALL_JOBS = {
    "payment_reminders": send_payment_reminders,
    "payment_expiry": expire_payment_links,
    "feedback_requests": send_feedback_requests,
    "courier_dispatch": dispatch_couriers,
    "kitchen_alerts": send_kitchen_alerts,
    "slot_generation": generate_upcoming_slots,
    "settings_refresh": refresh_settings,
}
