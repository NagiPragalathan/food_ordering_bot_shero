"""Zoho CRM synchronisation (spec section 2).

Design rule for this whole module: **CRM sync never breaks the conversation.**
Zoho is the reporting system, not the system of record for an order. If it is
down or misconfigured, the customer must still be able to browse, pay and eat.
So every function here catches integration failures, logs them loudly, and
returns a success flag the caller may ignore.

The local database keeps the authoritative stage and timestamps, which means a
failed push can be replayed later without data loss (the admin's Push to
Zoho does exactly that).

A customer is a Lead until their first payment, then a Contact: Zoho's own
Lead conversion, as the spec says. The bot's fields have the same names on
both modules, so everything here works on "the customer's record" - a
(module, id) pair - whichever it is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConfigurationError, IntegrationError
from app.core.logging import get_logger
from app.db.models import (
    Customer,
    DeliverySlot,
    LeadStage,
    MenuItem,
    Order,
    OrderStage,
    Outlet,
)
from app.integrations.zoho import crm
from app.integrations.zoho import fields as f
from app.services import catalogue_sync
from app.services.customers import record_stage

log = get_logger(__name__)

ZOHO_ERRORS = (IntegrationError, ConfigurationError)

Record = tuple[str, str]        # (module, Zoho record id)


@dataclass
class OrderContext:
    """What Zoho needs about an order beyond the Order row itself: the
    kitchen (to link its Vendor), the slot's start time, and the menu rows of
    the dishes (to link their Products). Built by order_context from a
    session, so the sync functions never touch the database themselves."""
    outlet: Outlet | None = None
    delivery_at: datetime | None = None
    dishes: dict[str, MenuItem] = field(default_factory=dict)     # by retailer_id

    @property
    def outlet_name(self) -> str | None:
        return self.outlet.name if self.outlet else None


async def order_context(session: AsyncSession, order: Order) -> OrderContext:
    outlet = await session.get(Outlet, order.outlet_id) if order.outlet_id else None
    slot = await session.get(DeliverySlot, order.slot_id) if order.slot_id else None
    codes = [parsed[0] for line in order.items or []
             if (parsed := f.parse_line(line)) is not None]
    dishes = ((await session.execute(catalogue_sync.dishes_with_labels(codes))).scalars()
              if codes else [])
    return OrderContext(outlet=outlet, delivery_at=slot.starts_at if slot else None,
                        dishes={dish.retailer_id: dish for dish in dishes})


async def ensure_record(customer: Customer) -> Record | None:
    """The customer's Zoho record, creating a Lead when there is none.

    A paid customer is a Contact, so that module is checked first; someone
    may also already be in Zoho from a hand-typed entry. One record per
    WhatsApp number (spec section 2), never a duplicate.
    """
    if customer.zoho_contact_id:
        return (f.CONTACTS, customer.zoho_contact_id)
    if customer.zoho_lead_id:
        return (f.LEADS, customer.zoho_lead_id)

    try:
        for module in f.PERSON_MODULES:
            existing = await crm.find_by_phone(module, customer.whatsapp_number)
            if existing:
                _link(customer, module, existing)
                return (module, existing["id"])

        lead_id = await crm.create_lead(crm.new_lead_fields(
            whatsapp_number=customer.whatsapp_number,
            name=customer.name,
            email=customer.email,
            lead_source=customer.lead_source,
            ad_id=customer.ad_id,
            campaign_id=customer.campaign_id,
            stage=LeadStage(customer.lead_stage),
            history=crm.stage_history(customer.stage_timestamps or {}),
            whatsapp_profile_name=customer.whatsapp_profile_name,
        ))
        customer.zoho_lead_id = lead_id
        return (f.LEADS, lead_id) if lead_id else None

    except (*ZOHO_ERRORS, ValueError, KeyError) as exc:
        log.error("zoho_ensure_record_failed", customer_id=str(customer.id),
                  error=str(exc))
        return None


def _link(customer: Customer, module: str, record: dict) -> None:
    """Adopt a record Zoho already holds for this number (spec step 2).

    A Contact means they paid before. The name and email come back with it,
    so a returning customer is greeted by name even after the bot's own
    database was cleared.
    """
    if module == f.CONTACTS:
        customer.zoho_contact_id = record["id"]
        customer.is_converted = True
    else:
        customer.zoho_lead_id = record["id"]
    if not customer.name and f.join_name(record):
        customer.name = f.join_name(record)
    if not customer.email and record.get(f.EMAIL):
        customer.email = record[f.EMAIL]
    log.info("zoho_existing_record_linked", module=module, zoho_id=record["id"])


# The order a customer walks through the funnel. A stage outside it (Not
# Serviceable, Payment Abandoned / Failed, Converted) ends one attempt, and
# the next attempt may start again from anywhere.
FUNNEL: tuple[LeadStage, ...] = (
    LeadStage.NEW_ENQUIRY,
    LeadStage.DETAILS_CAPTURED,
    LeadStage.CUISINE_SELECTED,
    LeadStage.CART_CREATED,
    LeadStage.OUTLET_SELECTED,
    LeadStage.SLOT_SELECTED,
    LeadStage.PAYMENT_LINK_SENT,
)


def is_backwards(current: str | None, stage: LeadStage) -> bool:
    """True when `stage` is earlier in the funnel than where the customer is.

    The web page reports a stage on every cart edit and address check; a
    customer who adds one more dish after picking a slot has not gone back
    to "Cart Created", and the drop-off report must not say they did.
    """
    try:
        return FUNNEL.index(LeadStage(current)) > FUNNEL.index(stage)
    except ValueError:      # current or stage outside the funnel
        return False


async def advance_stage(customer: Customer, stage: LeadStage, *,
                        forward_only: bool = False) -> bool:
    """Record a stage locally, then mirror it to Zoho's Bot Stage.

    Returns True when the stage changed locally - the part that always
    succeeds and drives our own reporting. `forward_only` ignores a stage
    that would move the customer backwards (see is_backwards).
    """
    if forward_only and is_backwards(customer.lead_stage, stage):
        return False
    changed = record_stage(customer, stage)
    if not changed:
        return False

    record = await ensure_record(customer)
    if record:
        # Also right after creating: a record found by phone may be at any stage.
        await _update(customer, record, lambda module: _stage_fields(customer, module))
    return True


def note_passed(customer: Customer, stage: LeadStage) -> None:
    """Record a stage the customer passed without a step of its own.

    The web menu has no "pick a cuisine" screen - adding the first dish is
    both Cuisine Selected and Cart Created. Stamped locally only; the next
    advance_stage sends it to Zoho in the Bot Stage History, so it costs no
    extra Zoho call.
    """
    if not is_backwards(customer.lead_stage, stage):
        record_stage(customer, stage)


async def push_details(customer: Customer, **details) -> None:
    """Mirror captured details (name, email, address, outlet, ...) to Zoho."""
    record = await ensure_record(customer)
    if record:
        await _update(customer, record, lambda module: crm.detail_fields(module, **details))


async def convert_and_record_order(customer: Customer, order: Order,
                                   ctx: OrderContext) -> None:
    """Payment succeeded (spec step 16): Lead -> Contact, and file the Order
    with its Order Items."""
    record_stage(customer, LeadStage.CONVERTED)
    customer.is_converted = True

    details = _order_details(customer, order, ctx.outlet_name)
    record = await _ensure_converted(customer)
    if record:
        await _update(customer, record, lambda module: {
            **crm.detail_fields(module, **details), **_stage_fields(customer, module)})

    if order.zoho_order_id:
        return  # already filed; a webhook replay must not duplicate it
    await _file_order(customer, order, ctx)


async def push_order_stage(order: Order, stage: OrderStage, *,
                           delivered_at: datetime | None = None) -> None:
    """Mirror an order stage change into Zoho's Order Status (spec 17-18)."""
    if not order.zoho_order_id:
        log.info("zoho_order_stage_skipped", order_number=order.order_number,
                 reason="no zoho order id")
        return
    at = delivered_at or datetime.now(timezone.utc)
    try:
        await crm.update_order_stage(order.zoho_order_id, stage, at=at)
    except ZOHO_ERRORS as exc:
        log.error("zoho_order_stage_failed", order_number=order.order_number,
                  error=str(exc))
    status = crm.sales_order_status(stage)
    if order.zoho_sales_order_id and status:
        try:
            await crm.update_sales_order(order.zoho_sales_order_id, {f.SO_STATUS: status})
        except ZOHO_ERRORS as exc:
            log.error("zoho_sales_order_status_failed", order_number=order.order_number,
                      error=str(exc))


async def push_customer(customer: Customer, *, details: dict,
                        orders: list[tuple[Order, OrderContext]]) -> list[str]:
    """Admin "Push to Zoho": make Zoho match what we hold for one customer.

    The record gets the details, Bot Stage and history; a paid customer still
    held as a Lead is converted; each order in `orders` ((order, context)
    pairs) not yet in Zoho is filed, and one already there gets its kitchen,
    delivery time and Order Items brought up to date. Unlike the rest of this
    module the caller needs to know what failed, so problems come back as
    sentences (empty list = all went through) as well as being logged.
    """
    # A linked record may have been deleted in Zoho since (common while
    # testing). Check, and relink or recreate rather than updating a ghost.
    for module, attr in ((f.CONTACTS, "zoho_contact_id"), (f.LEADS, "zoho_lead_id")):
        zoho_id = getattr(customer, attr)
        if not zoho_id:
            continue
        try:
            if await crm.get_record(module, zoho_id) is None:
                log.info("zoho_linked_record_gone", module=module, zoho_id=zoho_id)
                setattr(customer, attr, None)
        except ZOHO_ERRORS as exc:
            log.error("zoho_record_check_failed", module=module, zoho_id=zoho_id,
                      error=str(exc))
            return [f"Could not reach Zoho: {exc}"]

    was_contact = bool(customer.zoho_contact_id)
    record = (await _ensure_converted(customer) if customer.is_converted
              else await ensure_record(customer))
    if record is None:
        return ["Could not find or create the Zoho record - see the server log."]

    problems: list[str] = []
    try:
        record = await _write(customer, record, lambda module: {
            **crm.detail_fields(module, **details), **_stage_fields(customer, module)})
    except (*ZOHO_ERRORS, ValueError) as exc:
        log.error("zoho_push_record_failed", module=record[0], zoho_id=record[1],
                  error=str(exc))
        problems.append(f"{record[0][:-1]} update failed: {exc}")
    if customer.is_converted and record[0] == f.LEADS:
        problems.append("Could not convert the Lead to a Contact - see the server log.")

    if customer.zoho_contact_id and not was_contact:
        # Orders filed while the customer was still a Lead point at the Lead.
        for order, _ in orders:
            if not order.zoho_order_id:
                continue
            try:
                await crm.relink_order(order.zoho_order_id, customer.zoho_contact_id)
            except ZOHO_ERRORS as exc:
                log.error("zoho_order_relink_failed", order_number=order.order_number,
                          error=str(exc))
                problems.append(f"Order {order.order_number} could not be moved to the "
                                "Contact - see the server log.")

    for order, ctx in orders:
        problem = (await _refresh_order(customer, order, ctx) if order.zoho_order_id
                   else await _file_order(customer, order, ctx))
        if problem:
            problems.append(problem)
    return problems


# --- helpers -----------------------------------------------------------------
async def _ensure_converted(customer: Customer) -> Record | None:
    """The customer's Contact, converting their Lead if they still are one.

    The callers write the bot's fields to the Contact right after, so the
    outcome does not depend on the CRM's conversion-mapping settings. When
    Zoho refuses, the customer stays a Lead with Bot Stage Converted rather
    than losing the stage; the admin's Push to Zoho retries the conversion.
    """
    record = await ensure_record(customer)
    if record is None or record[0] == f.CONTACTS:
        return record

    _, lead_id = record
    try:
        contact_id = await crm.convert_lead(lead_id)
    except ZOHO_ERRORS as exc:
        log.error("zoho_lead_convert_failed", lead_id=lead_id, error=str(exc))
        return record
    customer.zoho_contact_id = contact_id
    customer.zoho_lead_id = None        # Zoho retires the Lead on conversion
    return (f.CONTACTS, contact_id)


async def _file_order(customer: Customer, order: Order, ctx: OrderContext) -> str | None:
    """File the Order, linked to its kitchen's Vendor, then its Order Items.

    Returns a problem sentence for the admin, or None when all went through.
    """
    vendor_id = await catalogue_sync.ensure_vendor(ctx.outlet) if ctx.outlet else None
    try:
        order.zoho_order_id = await crm.create_order(crm.order_fields(
            order, zoho_contact_id=customer.zoho_contact_id,
            zoho_lead_id=customer.zoho_lead_id, outlet_name=ctx.outlet_name,
            cuisine=customer.cuisine_preference, zoho_vendor_id=vendor_id,
            delivery_at=ctx.delivery_at,
        ))
    except (*ZOHO_ERRORS, ValueError) as exc:
        log.error("zoho_order_create_failed", order_number=order.order_number,
                  error=str(exc))
        order.zoho_order_id = None
    zoho_order_id = order.zoho_order_id
    if not zoho_order_id:
        return f"Order {order.order_number} could not be filed - see the server log."
    return await _file_items_and_sales_order(customer, order, ctx, zoho_order_id)


async def _file_items_and_sales_order(customer: Customer, order: Order, ctx: OrderContext,
                                      zoho_order_id: str) -> str | None:
    """Order Items first (they sync the Products), then the Sales Order."""
    problems = [p for p in (await _file_order_items(customer, order, ctx, zoho_order_id),
                            await _file_sales_order(customer, order, ctx, zoho_order_id)) if p]
    return " ".join(problems) or None


async def _file_sales_order(customer: Customer, order: Order, ctx: OrderContext,
                            zoho_order_id: str) -> str | None:
    """The order as a Zoho Sales Order, with its product grid, linked to the
    Contact and to the Order (the Order's Sales Order lookup). A second call
    brings the Contact link and status up to date instead of filing again."""
    product_ids = {code: dish.zoho_product_id for code, dish in ctx.dishes.items()
                   if dish.zoho_product_id}
    fields = crm.sales_order_fields(order, product_ids=product_ids,
                                    zoho_contact_id=customer.zoho_contact_id,
                                    delivery_at=ctx.delivery_at)
    if fields is None:
        log.info("zoho_sales_order_skipped", order_number=order.order_number,
                 reason="no dish has a Zoho Product")
        return None
    try:
        if order.zoho_sales_order_id:
            await crm.update_sales_order(order.zoho_sales_order_id, {
                f.SO_CONTACT: fields[f.SO_CONTACT], f.SO_STATUS: fields[f.SO_STATUS]})
        else:
            order.zoho_sales_order_id = await crm.create_sales_order(fields)
        if order.zoho_sales_order_id:
            await crm.update_order(zoho_order_id,
                                   {f.O_SALES_ORDER: {"id": order.zoho_sales_order_id}})
    except ZOHO_ERRORS as exc:
        log.error("zoho_sales_order_failed", order_number=order.order_number, error=str(exc))
        return (f"Order {order.order_number} could not be filed as a Sales Order "
                "- see the server log.")
    return None


async def _refresh_order(customer: Customer, order: Order, ctx: OrderContext) -> str | None:
    """An Order already in Zoho: give it the kitchen and delivery time, and
    file or update its Order Items (the admin's Push to Zoho)."""
    zoho_order_id = order.zoho_order_id
    if not zoho_order_id:
        return await _file_order(customer, order, ctx)
    vendor_id = await catalogue_sync.ensure_vendor(ctx.outlet) if ctx.outlet else None
    try:
        await crm.update_order(zoho_order_id, crm.order_link_fields(
            zoho_vendor_id=vendor_id, outlet_name=ctx.outlet_name,
            delivery_at=ctx.delivery_at))
    except ZOHO_ERRORS as exc:
        log.error("zoho_order_update_failed", order_number=order.order_number,
                  error=str(exc))
        return f"Order {order.order_number} could not be updated - see the server log."
    return await _file_items_and_sales_order(customer, order, ctx, zoho_order_id)


async def _file_order_items(customer: Customer, order: Order, ctx: OrderContext,
                            zoho_order_id: str) -> str | None:
    """One Order Item per cart line, linked to its Product and the Contact.

    Lines filed before are updated (order.zoho_item_ids remembers them), so
    a push after the customer became a Contact links them to it, and a dish
    synced to Products later gets linked too. A dish no longer on the menu
    is filed without a Product link; its name and price are on the item.
    """
    failed = []
    for line in order.items or []:
        parsed = f.parse_line(line)
        if parsed is None:
            continue
        code, name = parsed[0], parsed[1]
        dish = ctx.dishes.get(code)
        product_id = await catalogue_sync.ensure_product(dish) if dish else None
        fields = crm.order_item_fields(order, line, zoho_order_id=zoho_order_id,
                                       zoho_product_id=product_id,
                                       zoho_contact_id=customer.zoho_contact_id)
        if fields is None:
            continue
        try:
            filed = (order.zoho_item_ids or {}).get(code)
            if filed:
                await crm.update_order_item(filed, fields)
            else:
                item_id = await crm.create_order_item(fields)
                if item_id:
                    # A new dict, so SQLAlchemy notices the JSON column changed.
                    order.zoho_item_ids = {**(order.zoho_item_ids or {}), code: item_id}
        except ZOHO_ERRORS as exc:
            log.error("zoho_order_item_failed", order_number=order.order_number,
                      dish=code, error=str(exc))
            failed.append(name)
    if failed:
        return (f"Order {order.order_number}: {', '.join(failed)} could not be filed in "
                "Order Items - see the server log.")
    return None


def _order_details(customer: Customer, order: Order, outlet_name: str | None) -> dict:
    """What the Contact should hold after this order: the address it went to."""
    return {
        "name": customer.name, "email": customer.email,
        "whatsapp_profile_name": customer.whatsapp_profile_name,
        "address": order.delivery_address, "apartment_unit": order.apartment_unit,
        "postal_code": order.postal_code,
        "latitude": order.delivery_latitude, "longitude": order.delivery_longitude,
        "outlet_name": outlet_name,
        "distance_km": order.distance_km if order.distance_km is not None else customer.distance_km,
        "cuisine": customer.cuisine_preference,
    }


def _stage_fields(customer: Customer, module: str) -> dict:
    return crm.stage_fields(module, LeadStage(customer.lead_stage),
                            crm.stage_history(customer.stage_timestamps or {}))


async def _write(customer: Customer, record: Record, build) -> Record:
    """Write `build(module)` to the record; returns the record written.

    A Lead can be converted behind the bot's back - by staff pressing
    Convert in Zoho, say. Zoho then refuses every write to it, so the bot
    follows the Lead to the Contact it became (found by phone) and writes
    there, with the fields rebuilt for that module.
    """
    module, zoho_id = record
    try:
        await crm.update_record(module, zoho_id, build(module))
        return record
    except IntegrationError as exc:
        if module != f.LEADS or not crm.is_converted_error(exc):
            raise
    log.info("zoho_lead_converted_elsewhere", zoho_id=zoho_id)
    customer.zoho_lead_id = None
    followed = await ensure_record(customer)
    if followed is None or followed[0] != f.CONTACTS:
        raise IntegrationError("zoho", f"Lead {zoho_id} was converted, but no Contact "
                                       "with the customer's number was found")
    await crm.update_record(*followed, build(f.CONTACTS))
    return followed


async def _update(customer: Customer, record: Record, build) -> None:
    """_write, with the failure logged instead of raised."""
    try:
        await _write(customer, record, build)
    except ZOHO_ERRORS as exc:
        log.error("zoho_record_update_failed", module=record[0], zoho_id=record[1],
                  error=str(exc))
