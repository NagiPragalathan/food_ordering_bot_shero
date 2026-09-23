"""Order lifecycle.

Creation of the draft order at summary time (spec step 14), then the stage
transitions that run from payment through delivery (steps 16-19).
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Customer, Order, OrderStage, Outlet, PaymentStatus
from app.services.pricing import PricedOrder

log = get_logger(__name__)

ORDER_PREFIX = "SHO"
# Crockford-style alphabet: no I, L, O, U, so an order number read aloud over
# the phone cannot be mistyped.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
SUFFIX_LENGTH = 5
MAX_NUMBER_ATTEMPTS = 8


def generate_order_number() -> str:
    """e.g. SHO-260922-K4T9P (date + random suffix)."""
    stamp = datetime.now(timezone.utc).strftime("%y%m%d")
    suffix = "".join(secrets.choice(ALPHABET) for _ in range(SUFFIX_LENGTH))
    return f"{ORDER_PREFIX}-{stamp}-{suffix}"


async def allocate_order_number(session: AsyncSession) -> str:
    """A number not already used.

    Collisions are vanishingly unlikely (32^5 per day) but a duplicate would
    break the Stripe link and the WhatsApp templates, so it is checked.
    """
    for _ in range(MAX_NUMBER_ATTEMPTS):
        candidate = generate_order_number()
        exists = await session.execute(
            select(Order.id).where(Order.order_number == candidate)
        )
        if exists.scalar_one_or_none() is None:
            return candidate
    raise RuntimeError("could not allocate a unique order number")


async def create_draft_order(
    session: AsyncSession,
    *,
    customer: Customer,
    outlet: Outlet,
    priced: PricedOrder,
    slot_id: uuid.UUID | str | None = None,
    slot_starts_at: datetime | None = None,
    slot_ends_at: datetime | None = None,
    slot_label: str | None = None,
    distance_km: float | None = None,
) -> Order:
    """Create the unpaid order behind the summary message."""
    order = Order(
        order_number=await allocate_order_number(session),
        customer_id=customer.id,
        outlet_id=outlet.id,
        slot_id=uuid.UUID(str(slot_id)) if slot_id else None,
        items=priced.items_as_dicts(),
        item_count=priced.item_count,
        dish_total=priced.dish_total,
        delivery_fee=priced.delivery_fee,
        extra_fees=priced.extra_fees,
        tax=priced.tax,
        total=priced.total,
        currency=priced.currency,
        delivery_address=customer.address_line1,
        apartment_unit=customer.apartment_unit,
        delivery_instructions=customer.delivery_instructions,
        contact_number=customer.contact_number or customer.whatsapp_number,
        postal_code=customer.postal_code,
        delivery_latitude=customer.latitude,
        delivery_longitude=customer.longitude,
        distance_km=distance_km if distance_km is not None else customer.distance_km,
        slot_starts_at=slot_starts_at,
        slot_ends_at=slot_ends_at,
        slot_label=slot_label,
        payment_status=PaymentStatus.NOT_STARTED,
        stage=OrderStage.PENDING_PAYMENT,
        stage_timestamps={str(OrderStage.PENDING_PAYMENT): _now_iso()},
        uber_quote_id=priced.uber_quote_id,
        uber_quote_raw=priced.uber_quote_raw,
    )
    session.add(order)
    await session.flush()
    log.info("order_created", order_number=order.order_number,
             total=str(order.total), outlet=outlet.code)
    return order


def set_stage(order: Order, stage: OrderStage) -> bool:
    """Move the order to a new stage, stamping the transition time.

    Returns True only on a real change, so the caller does not re-notify the
    customer or re-push to Zoho for a repeated webhook.
    """
    if order.stage == str(stage):
        return False

    order.stage = str(stage)
    now = datetime.now(timezone.utc)
    order.stage_timestamps = {**(order.stage_timestamps or {}), str(stage): _now_iso()}

    # Keep the dedicated columns in step for reporting and for Zoho.
    match stage:
        case OrderStage.PAID_SLOT_BOOKED:
            order.paid_at = order.paid_at or now
        case OrderStage.SENT_TO_KITCHEN:
            order.sent_to_kitchen_at = order.sent_to_kitchen_at or now
        case OrderStage.OUT_FOR_DELIVERY:
            order.out_for_delivery_at = order.out_for_delivery_at or now
        case OrderStage.DELIVERED:
            order.delivered_at = order.delivered_at or now
        case OrderStage.CANCELLED | OrderStage.REFUNDED:
            order.cancelled_at = order.cancelled_at or now

    log.info("order_stage_changed", order_number=order.order_number, stage=str(stage))
    return True


# --- lookups -----------------------------------------------------------------
async def get_order(session: AsyncSession, order_id: str | uuid.UUID) -> Order | None:
    try:
        return await session.get(Order, uuid.UUID(str(order_id)))
    except ValueError:
        return None


async def get_by_number(session: AsyncSession, order_number: str) -> Order | None:
    result = await session.execute(
        select(Order).where(Order.order_number == order_number)
    )
    return result.scalar_one_or_none()


async def get_by_stripe_session(session: AsyncSession, session_id: str) -> Order | None:
    result = await session.execute(
        select(Order).where(Order.stripe_session_id == session_id)
    )
    return result.scalar_one_or_none()


async def get_by_payment_intent(session: AsyncSession,
                                payment_intent_id: str) -> Order | None:
    result = await session.execute(
        select(Order).where(Order.stripe_payment_intent_id == payment_intent_id)
    )
    return result.scalar_one_or_none()


async def get_latest_for_customer(session: AsyncSession,
                                  customer_id: uuid.UUID) -> Order | None:
    result = await session.execute(
        select(Order)
        .where(Order.customer_id == customer_id)
        .order_by(Order.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def find_orders_for_reminder(session: AsyncSession,
                                   now: datetime | None = None) -> list[Order]:
    """Unpaid orders whose reminder is due (spec step 15, 15 minutes)."""
    from datetime import timedelta

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=settings.payment_reminder_minutes)
    result = await session.execute(
        select(Order).where(
            Order.payment_status == PaymentStatus.LINK_SENT,
            Order.reminder_sent_at.is_(None),
            Order.created_at <= cutoff,
            Order.payment_link_expires_at > now,
        )
    )
    return list(result.scalars())


async def find_expired_orders(session: AsyncSession,
                              now: datetime | None = None) -> list[Order]:
    """Unpaid orders whose payment link has lapsed (spec step 15)."""
    now = now or datetime.now(timezone.utc)
    result = await session.execute(
        select(Order).where(
            Order.payment_status == PaymentStatus.LINK_SENT,
            Order.payment_link_expires_at <= now,
        )
    )
    return list(result.scalars())


async def find_orders_for_feedback(session: AsyncSession,
                                   now: datetime | None = None) -> list[Order]:
    """Delivered orders due a feedback request (spec step 19)."""
    from datetime import timedelta

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=settings.feedback_delay_minutes)
    result = await session.execute(
        select(Order).where(
            Order.stage == OrderStage.DELIVERED,
            Order.delivered_at <= cutoff,
            Order.feedback_requested_at.is_(None),
        )
    )
    return list(result.scalars())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
