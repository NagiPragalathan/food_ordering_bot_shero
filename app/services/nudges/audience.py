"""Who qualifies for a campaign right now - straight from the local database.

Zoho holds the same customers, but asking it per run would be slow and
rate-limited; every fact needed here (carts, orders, sign-up dates, stages)
is already in our own tables, indexed. Each finder is one or two queries,
however many customers there are.

A finder returns Candidates: the customer, what the nudge is about
(`dedupe_key`, so the same cart is never nudged twice) and the facts the
audience filters and the message placeholders need.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Conversation,
    Customer,
    InboundMessage,
    NudgeKind,
    Order,
    OrderStage,
    Outlet,
    PaymentStatus,
)
from app.services import cart as cart_service
from app.services.nudges.config import Audience, Trigger

CHUNK = 500
ZERO = Decimal("0.00")
# A paid order still on its way: the customer is not "lapsed".
ACTIVE_STAGES = (OrderStage.PAID_SLOT_BOOKED, OrderStage.SENT_TO_KITCHEN,
                 OrderStage.OUT_FOR_DELIVERY)


def aware(moment: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; everything here is UTC."""
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def digits(raw: str | None) -> str:
    return "".join(ch for ch in (raw or "") if ch.isdigit())


@dataclass
class PastOrder:
    id: uuid.UUID
    order_number: str
    total: Decimal
    paid_at: datetime | None
    items: list
    outlet_id: uuid.UUID | None


@dataclass
class Facts:
    """A customer's paid history, newest order first."""

    orders: list[PastOrder] = field(default_factory=list)

    @property
    def paid_orders(self) -> int:
        return len(self.orders)

    @property
    def total_spent(self) -> Decimal:
        return sum((o.total for o in self.orders), ZERO)

    @property
    def last(self) -> PastOrder | None:
        return self.orders[0] if self.orders else None

    @property
    def last_paid_at(self) -> datetime | None:
        return self.last.paid_at if self.last else None

    def dishes(self) -> set[str]:
        names = set()
        for order in self.orders:
            for line in order.items or []:
                for key in ("name", "retailer_id"):
                    if line.get(key):
                        names.add(" ".join(str(line[key]).lower().split()))
        return names


@dataclass
class Candidate:
    customer: Customer
    dedupe_key: str
    # When the trigger happened (cart left, link expired, last order...).
    since: datetime | None
    facts: Facts = field(default_factory=Facts)
    conversation: Conversation | None = None
    cart: list[dict] = field(default_factory=list)
    order: Order | None = None          # the unpaid order, for abandoned_checkout


# --- shared loaders -------------------------------------------------------------------
def _chunks(ids: list) -> list[list]:
    return [ids[i:i + CHUNK] for i in range(0, len(ids), CHUNK)]


async def load_facts(session: AsyncSession, customer_ids: list[uuid.UUID]) -> dict[uuid.UUID, Facts]:
    facts: dict[uuid.UUID, Facts] = {cid: Facts() for cid in customer_ids}
    for chunk in _chunks(list(customer_ids)):
        rows = await session.execute(
            select(Order.id, Order.customer_id, Order.order_number, Order.total,
                   Order.paid_at, Order.items, Order.outlet_id)
            .where(Order.customer_id.in_(chunk), Order.payment_status == PaymentStatus.PAID)
            .order_by(Order.paid_at.desc()))
        for row in rows:
            facts[row.customer_id].orders.append(PastOrder(
                id=row.id, order_number=row.order_number, total=row.total or ZERO,
                paid_at=aware(row.paid_at), items=row.items or [], outlet_id=row.outlet_id))
    return facts


async def conversations_of(session: AsyncSession, customer_ids: list[uuid.UUID]) -> dict[uuid.UUID, Conversation]:
    found: dict[uuid.UUID, Conversation] = {}
    for chunk in _chunks(list(customer_ids)):
        rows = await session.execute(select(Conversation).where(Conversation.customer_id.in_(chunk)))
        found.update({c.customer_id: c for c in rows.scalars()})
    return found


async def busy_customers(session: AsyncSession, customer_ids: list[uuid.UUID],
                         now: datetime) -> set[uuid.UUID]:
    """Customers with an order under way: a payment link still open, or a paid
    order not yet delivered. "Come back and order" would be wrong for them."""
    busy: set[uuid.UUID] = set()
    recent = now - timedelta(hours=12)
    for chunk in _chunks(list(customer_ids)):
        rows = await session.execute(
            select(Order.customer_id).where(
                Order.customer_id.in_(chunk),
                or_(and_(Order.payment_status == PaymentStatus.LINK_SENT,
                         Order.payment_link_expires_at > now),
                    and_(Order.payment_status == PaymentStatus.PAID,
                         Order.stage.in_([str(s) for s in ACTIVE_STAGES]),
                         func.coalesce(Order.slot_ends_at, Order.paid_at) >= recent))))
        busy.update(rows.scalars())
    return busy


async def last_inbound(session: AsyncSession, now: datetime) -> dict[str, datetime]:
    """Each number's last message to us in the past day - the 24-hour window.

    Read from the webhook's message log, which only holds messages customers
    sent; a message we sent does not open the window.
    """
    rows = await session.execute(
        select(InboundMessage.whatsapp_number, func.max(InboundMessage.created_at))
        .where(InboundMessage.created_at >= now - timedelta(hours=24))
        .group_by(InboundMessage.whatsapp_number))
    latest: dict[str, datetime] = {}
    for number, at in rows:
        key, at = digits(number), aware(at)
        if key and (key not in latest or at > latest[key]):
            latest[key] = at
    return latest


async def outlets_by_id(session: AsyncSession) -> dict[uuid.UUID, Outlet]:
    return {o.id: o for o in (await session.execute(select(Outlet))).scalars()}


# --- the finders ------------------------------------------------------------------------
async def find(session: AsyncSession, kind: str, trigger: Trigger, audience: Audience,
               now: datetime) -> list[Candidate]:
    finder = FINDERS[NudgeKind(kind)]
    candidates = await finder(session, trigger, audience, now)
    facts = await load_facts(session, [c.customer.id for c in candidates])
    for candidate in candidates:
        candidate.facts = facts[candidate.customer.id]
    _finish(candidates)
    return candidates


def _finish(candidates: list[Candidate]) -> None:
    """Keys that depend on the facts, once they are loaded."""
    for candidate in candidates:
        if candidate.dedupe_key == "after-last-order":
            last = candidate.facts.last
            candidate.dedupe_key = f"after:{last.id}" if last else "after:none"


def _cart_key(lines: list[dict]) -> str:
    signature = sorted((str(line.get("retailer_id")), int(line.get("quantity") or 0))
                       for line in lines)
    return "cart:" + hashlib.sha1(json.dumps(signature).encode()).hexdigest()[:16]


def _cart_time(conversation: Conversation) -> datetime | None:
    stamped = conversation.get("cart_updated_at")
    if stamped:
        try:
            return aware(datetime.fromisoformat(stamped))
        except (TypeError, ValueError):
            pass
    return aware(conversation.updated_at)


async def _abandoned_carts(session, trigger: Trigger, audience: Audience,
                           now: datetime) -> list[Candidate]:
    oldest = now - timedelta(hours=trigger.within_hours)
    newest = now - timedelta(minutes=trigger.after_minutes)
    rows = await session.execute(
        select(Conversation, Customer).join(Customer, Customer.id == Conversation.customer_id)
        # A cart changed at T means updated_at >= T: older rows cannot qualify.
        .where(Conversation.updated_at >= oldest))
    found = []
    for conversation, customer in rows:
        lines = cart_service.read(conversation)
        if not lines or cart_service.item_count(lines) < (trigger.min_items or 1):
            continue
        # A cart a nudge refilled from an unpaid order, untouched since: that
        # order was already chased.
        restored = conversation.get("cart_restored_at")
        if restored and restored == conversation.get("cart_updated_at"):
            continue
        if trigger.min_value is not None and cart_service.subtotal(lines) < trigger.min_value:
            continue
        left_at = _cart_time(conversation)
        if left_at is None or not oldest <= left_at <= newest:
            continue
        # Still chatting: not abandoned yet.
        talked = aware(conversation.last_message_at)
        if talked is not None and talked > newest:
            continue
        found.append(Candidate(customer=customer, dedupe_key=_cart_key(lines), since=left_at,
                               conversation=conversation, cart=lines))

    # An order placed after the cart was last touched means it was not abandoned.
    placed = await _latest_order_times(session, [c.customer.id for c in found], oldest)
    return [c for c in found if not (placed.get(c.customer.id) and placed[c.customer.id] >= c.since)]


async def _latest_order_times(session, customer_ids, since: datetime) -> dict[uuid.UUID, datetime]:
    latest: dict[uuid.UUID, datetime] = {}
    for chunk in _chunks(list(customer_ids)):
        rows = await session.execute(
            select(Order.customer_id, func.max(Order.created_at))
            .where(Order.customer_id.in_(chunk), Order.created_at >= since)
            .group_by(Order.customer_id))
        latest.update({cid: aware(at) for cid, at in rows})
    return latest


async def _abandoned_checkouts(session, trigger: Trigger, audience: Audience,
                               now: datetime) -> list[Candidate]:
    oldest = now - timedelta(hours=trigger.within_hours)
    newest = now - timedelta(minutes=trigger.after_minutes)
    statuses = [str(PaymentStatus(s)) for s in (trigger.payment_statuses or ["expired", "failed"])]
    rows = await session.execute(
        select(Order, Customer).join(Customer, Customer.id == Order.customer_id)
        # The status changed when the link lapsed or failed, so updated_at is
        # never earlier than that moment.
        .where(Order.payment_status.in_(statuses), Order.updated_at >= oldest)
        .order_by(Order.created_at.desc()))
    latest: dict[uuid.UUID, Candidate] = {}
    for order, customer in rows:
        if customer.id in latest:
            continue           # their newest unpaid order is the one to mention
        if trigger.min_value is not None and (order.total or ZERO) < trigger.min_value:
            continue
        left_at = aware(order.payment_link_expires_at
                        if order.payment_status == PaymentStatus.EXPIRED
                        and order.payment_link_expires_at else order.updated_at)
        if left_at is None or not oldest <= left_at <= newest:
            continue
        latest[customer.id] = Candidate(customer=customer, dedupe_key=f"order:{order.id}",
                                        since=left_at, order=order)

    # Ordered again since (paid, or a link sent): nothing to chase.
    retried: set[uuid.UUID] = set()
    went_through = [PaymentStatus.PAID, PaymentStatus.LINK_SENT, PaymentStatus.REFUNDED]
    for chunk in _chunks(list(latest)):
        rows = await session.execute(
            select(Order.customer_id, Order.created_at)
            .where(Order.customer_id.in_(chunk), Order.created_at >= oldest,
                   Order.payment_status.in_([str(s) for s in went_through])))
        for customer_id, created in rows:
            if aware(created) > aware(latest[customer_id].order.created_at):
                retried.add(customer_id)
    return [c for cid, c in latest.items() if cid not in retried]


async def _repeat_purchases(session, trigger: Trigger, audience: Audience,
                            now: datetime) -> list[Candidate]:
    last_paid = func.max(Order.paid_at)
    query = (select(Order.customer_id, last_paid)
             .where(Order.payment_status == PaymentStatus.PAID, Order.paid_at.is_not(None))
             .group_by(Order.customer_id)
             .having(last_paid <= now - timedelta(days=trigger.days_since_last_order)))
    if trigger.max_days_since_last_order:
        query = query.having(last_paid >= now - timedelta(days=trigger.max_days_since_last_order))
    rows = list(await session.execute(query))
    customers = await _customers(session, [cid for cid, _ in rows])
    return [Candidate(customer=customers[cid], dedupe_key="after-last-order", since=aware(at))
            for cid, at in rows if cid in customers]


async def _never_ordered(session, trigger: Trigger, audience: Audience,
                         now: datetime) -> list[Candidate]:
    paid = exists().where(Order.customer_id == Customer.id,
                          Order.payment_status.in_([PaymentStatus.PAID, PaymentStatus.REFUNDED]))
    query = select(Customer).where(
        ~paid, Customer.created_at <= now - timedelta(days=trigger.days_since_signup))
    if trigger.max_days_since_signup:
        query = query.where(Customer.created_at >= now - timedelta(days=trigger.max_days_since_signup))
    if trigger.require_details:
        query = query.where(Customer.name.is_not(None), Customer.email.is_not(None))
    return [Candidate(customer=c, dedupe_key="signup", since=aware(c.created_at))
            for c in (await session.execute(query)).scalars()]


async def _everyone(session, trigger: Trigger, audience: Audience,
                    now: datetime) -> list[Candidate]:
    """Every customer, narrowed in SQL by the filters that live on the row."""
    query = select(Customer)
    if audience.lead_stages:
        query = query.where(Customer.lead_stage.in_(audience.lead_stages))
    if audience.has_email is True:
        query = query.where(Customer.email.is_not(None), Customer.email != "")
    if audience.signed_up_within_days:
        query = query.where(Customer.created_at >= now - timedelta(days=audience.signed_up_within_days))
    if audience.signed_up_older_than_days:
        query = query.where(Customer.created_at <= now - timedelta(days=audience.signed_up_older_than_days))
    if audience.whatsapp_numbers:
        query = query.where(Customer.whatsapp_number.in_(audience.whatsapp_numbers))
    return [Candidate(customer=c, dedupe_key="offer", since=aware(c.created_at))
            for c in (await session.execute(query.order_by(Customer.created_at))).scalars()]


async def _customers(session, ids: list[uuid.UUID]) -> dict[uuid.UUID, Customer]:
    found: dict[uuid.UUID, Customer] = {}
    for chunk in _chunks(ids):
        rows = await session.execute(select(Customer).where(Customer.id.in_(chunk)))
        found.update({c.id: c for c in rows.scalars()})
    return found


FINDERS = {
    NudgeKind.ABANDONED_CART: _abandoned_carts,
    NudgeKind.ABANDONED_CHECKOUT: _abandoned_checkouts,
    NudgeKind.REPEAT_PURCHASE: _repeat_purchases,
    NudgeKind.NEVER_ORDERED: _never_ordered,
    NudgeKind.TARGETED_OFFER: _everyone,
}


# --- the audience filters ------------------------------------------------------------------
def kitchen_of(candidate: Candidate, outlets: dict[uuid.UUID, Outlet]) -> Outlet | None:
    """Their chosen kitchen, else the one their last order came from."""
    outlet_id = candidate.customer.preferred_outlet_id
    if outlet_id is None and candidate.order is not None:
        outlet_id = candidate.order.outlet_id
    if outlet_id is None and candidate.facts.last is not None:
        outlet_id = candidate.facts.last.outlet_id
    return outlets.get(outlet_id) if outlet_id else None


def audience_problem(candidate: Candidate, audience: Audience, *, now: datetime,
                     outlets: dict[uuid.UUID, Outlet]) -> str | None:
    """Which filter leaves this customer out, or None if they are in."""
    customer, facts = candidate.customer, candidate.facts
    number = digits(customer.whatsapp_number)

    if audience.whatsapp_numbers and number not in audience.whatsapp_numbers:
        return "whatsapp_numbers"
    if number in audience.exclude_whatsapp_numbers:
        return "exclude_whatsapp_numbers"
    if audience.country_codes and not any(number.startswith(c) for c in audience.country_codes):
        return "country_codes"
    if any(number.startswith(c) for c in audience.exclude_country_codes):
        return "exclude_country_codes"

    if audience.min_paid_orders is not None and facts.paid_orders < audience.min_paid_orders:
        return "min_paid_orders"
    if audience.max_paid_orders is not None and facts.paid_orders > audience.max_paid_orders:
        return "max_paid_orders"
    if audience.min_total_spent is not None and facts.total_spent < audience.min_total_spent:
        return "min_total_spent"
    if audience.max_total_spent is not None and facts.total_spent > audience.max_total_spent:
        return "max_total_spent"

    last = facts.last_paid_at
    if audience.last_order_within_days is not None and (
            last is None or last < now - timedelta(days=audience.last_order_within_days)):
        return "last_order_within_days"
    if audience.last_order_older_than_days is not None and (
            last is not None and last > now - timedelta(days=audience.last_order_older_than_days)):
        return "last_order_older_than_days"

    joined = aware(customer.created_at)
    if audience.signed_up_within_days is not None and (
            joined is None or joined < now - timedelta(days=audience.signed_up_within_days)):
        return "signed_up_within_days"
    if audience.signed_up_older_than_days is not None and (
            joined is not None and joined > now - timedelta(days=audience.signed_up_older_than_days)):
        return "signed_up_older_than_days"

    if audience.cuisines and " ".join((customer.cuisine_preference or "").lower().split()) \
            not in audience.cuisines:
        return "cuisines"
    if audience.kitchens:
        outlet = kitchen_of(candidate, outlets)
        if outlet is None or (outlet.code or "").upper() not in audience.kitchens:
            return "kitchens"
    if audience.postal_codes and not any(
            (customer.postal_code or "").upper().startswith(p) for p in audience.postal_codes):
        return "postal_codes"
    if audience.lead_stages and customer.lead_stage not in audience.lead_stages:
        return "lead_stages"
    if audience.lead_sources and (customer.lead_source or "").lower() not in audience.lead_sources:
        return "lead_sources"
    if audience.has_email is not None and bool(customer.email) != audience.has_email:
        return "has_email"
    if audience.ordered_dishes and not (facts.dishes() & set(audience.ordered_dishes)):
        return "ordered_dishes"
    return None
