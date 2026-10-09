"""Admin Customers page: everyone who has talked to the bot.

Three things sit on top of the list:

- **Verification.** A Zoho id the bot saved is only a claim: the record may
  have been deleted in Zoho, or belong to a CRM the bot has since left. The
  page checks the shown rows' links against Zoho (one call per module) and
  drops the dead ones, so what it says is true and the next push recreates
  the record. With no Zoho connection it says that instead of guessing.
- **Delete** removes a customer and everything the bot stored for them
  (conversation, saved addresses, orders and their slot holds), so the number
  starts from step 1 on its next message. Meant for clearing test data. The
  rows are deleted explicitly rather than by relying on ON DELETE CASCADE,
  because SQLite (the local run) does not enforce foreign keys by default and
  orders are RESTRICT anyway. Nothing is deleted in Zoho.
- **Push to Zoho** re-sends the record's details and stage, converts a paid
  customer still held as a Lead, files any paid order Zoho does not have
  yet, and brings a filed order's kitchen link and Order Items up to date
  (crm_sync.push_customer).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import (
    Conversation,
    Customer,
    CustomerAddress,
    Order,
    Outlet,
    PaymentStatus,
    SlotHold,
)
from app.integrations.zoho import crm
from app.integrations.zoho import fields as f
from app.services import addresses, crm_sync, slots, zoho_connect

log = get_logger(__name__)

PAGE_SIZE = 100

# What the page knows about a customer's Zoho link.
ZOHO_OK = "ok"                  # the linked record exists
ZOHO_MISSING = "missing"        # it was linked, but Zoho no longer has it (link dropped)
ZOHO_NONE = "none"              # never linked
ZOHO_UNVERIFIED = "unverified"  # linked, but Zoho could not be asked
ZOHO_OFFLINE = "offline"        # Zoho is not connected


@dataclass
class CustomerRow:
    customer: Customer
    step: str | None
    orders: int
    paid_orders: int
    unsynced_orders: int
    addresses: int
    zoho_state: str = ZOHO_UNVERIFIED

    @property
    def zoho_label(self) -> str | None:
        """What the customer is in Zoho: "Contact" once paid, else "Lead"."""
        if self.customer.zoho_contact_id:
            return "Contact"
        if self.customer.zoho_lead_id:
            return "Lead"
        return None

    @property
    def in_zoho(self) -> bool:
        """The link is confirmed and nothing paid is missing there."""
        return self.zoho_state == ZOHO_OK and not self.unsynced_orders

    @property
    def needs_push(self) -> bool:
        """Push to Zoho would change something."""
        if self.zoho_state in (ZOHO_NONE, ZOHO_MISSING):
            return True
        return self.zoho_state == ZOHO_OK and bool(self.unsynced_orders)


def zoho_connected() -> bool:
    """Credentials and a refresh token are in place."""
    return zoho_connect.is_connected()


# Filters and sorts the Customers page offers (the keys are the URL values).
ACTIVITY = {"ordered": "Has ordered", "paid": "Has paid", "none": "No orders yet"}
ZOHO_FILTERS = {"linked": "In Zoho", "unlinked": "Not in Zoho"}
SORTS = {"recent": "Last active", "newest": "Newest first", "oldest": "Oldest first",
         "name": "Name A-Z"}


def _filtered(stmt, query: str | None = None, *, stage: str | None = None,
              activity: str | None = None, zoho: str | None = None):
    """The page's search and filters as WHERE clauses (shared by list and count)."""
    if query and query.strip():
        term = f"%{query.strip().lstrip('+')}%"
        stmt = stmt.where(or_(Customer.whatsapp_number.ilike(term),
                              Customer.name.ilike(term),
                              Customer.email.ilike(term)))
    if stage:
        stmt = stmt.where(Customer.lead_stage == stage)
    has_order = select(Order.id).where(Order.customer_id == Customer.id)
    if activity == "ordered":
        stmt = stmt.where(has_order.exists())
    elif activity == "paid":
        stmt = stmt.where(has_order.where(
            Order.payment_status == str(PaymentStatus.PAID)).exists())
    elif activity == "none":
        stmt = stmt.where(~has_order.exists())
    linked = or_(Customer.zoho_contact_id.is_not(None), Customer.zoho_lead_id.is_not(None))
    if zoho == "linked":
        stmt = stmt.where(linked)
    elif zoho == "unlinked":
        stmt = stmt.where(~linked)
    return stmt


def _ordering(sort: str):
    last_active = func.coalesce(Customer.last_seen_at, Customer.created_at)
    return {
        "newest": (Customer.created_at.desc(),),
        "oldest": (Customer.created_at.asc(),),
        # Nameless customers last, then alphabetical.
        "name": (Customer.name.is_(None), func.lower(Customer.name), last_active.desc()),
    }.get(sort, (last_active.desc(),))


async def count_customers(session: AsyncSession, query: str | None = None, **filters) -> int:
    """How many customers the search and filters match (for the pager)."""
    stmt = _filtered(select(func.count()).select_from(Customer), query, **filters)
    return int((await session.execute(stmt)).scalar_one())


async def customer_stats(session: AsyncSession, *, since) -> dict[str, int]:
    """The numbers above the list: everyone, paying, new since `since`, not in Zoho."""
    count = lambda *where: select(func.count()).select_from(Customer).where(*where)  # noqa: E731
    paying = select(Order.id).where(Order.customer_id == Customer.id,
                                    Order.payment_status == str(PaymentStatus.PAID)).exists()
    unlinked = (Customer.zoho_contact_id.is_(None), Customer.zoho_lead_id.is_(None))
    results = {}
    for key, stmt in {"total": count(), "paying": count(paying),
                      "new": count(Customer.created_at >= since),
                      "unlinked": count(*unlinked)}.items():
        results[key] = int((await session.execute(stmt)).scalar_one())
    return results


async def list_customers(session: AsyncSession, query: str | None = None,
                         limit: int = PAGE_SIZE, *, verify: bool = False,
                         offset: int = 0, sort: str = "recent", stage: str | None = None,
                         activity: str | None = None, zoho: str | None = None,
                         ) -> list[CustomerRow]:
    """One page of customers, with the counts the page shows.

    Four grouped queries, not one per customer. `verify` adds one Zoho call
    per module to confirm the links (see verify_links); it runs before the
    counts, so an order whose Zoho record is gone counts as "to push".
    """
    stmt = _filtered(select(Customer), query, stage=stage, activity=activity, zoho=zoho)
    customers = list((await session.execute(
        stmt.order_by(*_ordering(sort)).offset(offset).limit(limit)
    )).scalars())
    if not customers:
        return []
    ids = [c.id for c in customers]

    if verify:
        states = await verify_links(session, customers)
    else:
        states = {c.id: ZOHO_UNVERIFIED if (c.zoho_contact_id or c.zoho_lead_id) else ZOHO_NONE
                  for c in customers}

    steps = {cid: step for cid, step in (await session.execute(
        select(Conversation.customer_id, Conversation.step)
        .where(Conversation.customer_id.in_(ids))
    )).all()}
    order_counts = await _counts(session, Order.customer_id, ids)
    paid = await _counts(session, Order.customer_id, ids,
                         Order.payment_status == str(PaymentStatus.PAID))
    unsynced = await _counts(session, Order.customer_id, ids,
                             Order.payment_status == str(PaymentStatus.PAID),
                             Order.zoho_order_id.is_(None))
    address_counts = await _counts(session, CustomerAddress.customer_id, ids)

    return [CustomerRow(customer=c, step=steps.get(c.id),
                        orders=order_counts.get(c.id, 0),
                        paid_orders=paid.get(c.id, 0),
                        unsynced_orders=unsynced.get(c.id, 0),
                        addresses=address_counts.get(c.id, 0),
                        zoho_state=states[c.id])
            for c in customers]


async def verify_links(session: AsyncSession,
                       customers: list[Customer]) -> dict[uuid.UUID, str]:
    """Confirm the customers' Zoho links, and their paid orders', really exist.

    A link Zoho no longer has is dropped here, so the badge is honest and the
    next push recreates the record. A Lead somebody converted by hand in Zoho
    drops out too, and the next sync finds the Contact by phone instead.
    """
    if not zoho_connected():
        return {c.id: ZOHO_OFFLINE for c in customers}

    states = {c.id: ZOHO_NONE for c in customers}
    groups = {
        f.CONTACTS: [c for c in customers if c.zoho_contact_id],
        f.LEADS: [c for c in customers if c.zoho_lead_id and not c.zoho_contact_id],
    }
    for module, group in groups.items():
        if not group:
            continue
        attr = "zoho_contact_id" if module == f.CONTACTS else "zoho_lead_id"
        found = await _existing(module, [getattr(c, attr) for c in group])
        for c in group:
            zoho_id = getattr(c, attr)
            if found is None:
                states[c.id] = ZOHO_UNVERIFIED
            elif zoho_id in found:
                states[c.id] = ZOHO_OK
            else:
                states[c.id] = ZOHO_MISSING
                setattr(c, attr, None)
                log.info("zoho_link_dropped", module=module, zoho_id=zoho_id,
                         customer_id=str(c.id))

    orders = list((await session.execute(
        select(Order).where(Order.customer_id.in_([c.id for c in customers]),
                            Order.zoho_order_id.is_not(None))
    )).scalars())
    if orders:
        found = await _existing(settings.zoho_orders_module,
                                [str(o.zoho_order_id) for o in orders])
        for order in orders:
            if found is not None and order.zoho_order_id not in found:
                log.info("zoho_order_link_dropped", order_number=order.order_number,
                         zoho_id=order.zoho_order_id)
                order.zoho_order_id = None
    await session.flush()
    return states


async def forget_zoho_links(session: AsyncSession) -> int:
    """Drop every saved Zoho id: they belong to a CRM the bot has left.

    Returns how many customers were linked. Push to Zoho, or the customer's
    next message, recreates the records in the new CRM.
    """
    result = await session.execute(
        update(Customer)
        .where(or_(Customer.zoho_lead_id.is_not(None), Customer.zoho_contact_id.is_not(None)))
        .values(zoho_lead_id=None, zoho_contact_id=None)
        .execution_options(synchronize_session="fetch")
    )
    await session.execute(
        update(Order).where(Order.zoho_order_id.is_not(None)).values(zoho_order_id=None)
        .execution_options(synchronize_session="fetch")
    )
    await session.flush()
    count = result.rowcount or 0
    log.info("zoho_links_forgotten", customers=count)
    return count


async def get(session: AsyncSession, customer_id: str) -> Customer | None:
    try:
        return await session.get(Customer, uuid.UUID(str(customer_id)))
    except ValueError:      # not a UUID - a hand-edited URL
        return None


async def delete_customer(session: AsyncSession, customer: Customer) -> dict:
    """Remove the customer and everything stored for them. Returns counts."""
    order_ids = list((await session.execute(
        select(Order.id).where(Order.customer_id == customer.id)
    )).scalars())

    # Give any held or booked delivery window back before the hold goes.
    for order_id in order_ids:
        await slots.release_holds_for_order(session, order_id, reason="deleted",
                                            include_booked=True)
    removed = {"orders": len(order_ids)}
    if order_ids:
        await session.execute(delete(SlotHold).where(SlotHold.order_id.in_(order_ids)))
        await session.execute(delete(Order).where(Order.id.in_(order_ids)))
    removed["addresses"] = (await session.execute(
        delete(CustomerAddress).where(CustomerAddress.customer_id == customer.id)
    )).rowcount or 0
    await session.execute(delete(Conversation).where(Conversation.customer_id == customer.id))
    await session.delete(customer)
    await session.flush()
    log.info("admin_customer_deleted", customer_id=str(customer.id), **removed)
    return removed


async def push_to_zoho(session: AsyncSession, customer: Customer) -> list[str]:
    """Send the customer and their paid orders to Zoho; returns the problems."""
    saved = await addresses.list_for(session, customer)
    outlet = (await session.get(Outlet, customer.preferred_outlet_id)
              if customer.preferred_outlet_id else None)
    details = {
        "name": customer.name,
        "email": customer.email,
        "whatsapp_profile_name": customer.whatsapp_profile_name,
        "outlet_name": outlet.name if outlet else None,
        "distance_km": customer.distance_km,
        "cuisine": customer.cuisine_preference,
        # The default address; the older single-address columns otherwise.
        **(addresses.crm_details(saved[0]) if saved else {
            "address": customer.address_line1,
            "apartment_unit": customer.apartment_unit,
            "postal_code": customer.postal_code,
            "latitude": customer.latitude,
            "longitude": customer.longitude,
        }),
    }

    paid = list((await session.execute(
        select(Order).where(Order.customer_id == customer.id,
                            Order.payment_status == str(PaymentStatus.PAID))
        .order_by(Order.created_at)
    )).scalars())
    orders = [(order, await crm_sync.order_context(session, order)) for order in paid]

    problems = await crm_sync.push_customer(customer, details=details, orders=orders)
    await session.flush()       # keep the record / Order ids that were just linked
    log.info("admin_customer_pushed", customer_id=str(customer.id),
             zoho_id=customer.zoho_contact_id or customer.zoho_lead_id,
             problems=len(problems))
    return problems


async def _existing(module: str, ids: list[str]) -> set[str] | None:
    """Ids Zoho still has, or None when it could not be asked."""
    try:
        return await crm.existing_ids(module, ids)
    except crm_sync.ZOHO_ERRORS as exc:
        log.error("zoho_link_check_failed", module=module, error=str(exc))
        return None


async def _counts(session: AsyncSession, column, ids, *conditions) -> dict:
    rows = await session.execute(
        select(column, func.count()).where(column.in_(ids), *conditions).group_by(column)
    )
    return {key: count for key, count in rows.all()}
