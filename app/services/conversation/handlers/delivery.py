"""Delivery details and slot selection (steps 12-13).

Step 12 asks four questions in a fixed order. Rather than four near-identical
handlers, the sequence is driven by `DELIVERY_DETAIL_SEQUENCE` and a cursor in
the conversation context, so reordering or dropping a question is a data
change.

The spec requires payment to be blocked until all four are filled, which is
enforced by `missing_details()` - checked again at summary time, not just here.
"""

from __future__ import annotations

import uuid

from app.core.exceptions import NoSlotsAvailableError
from app.core.logging import get_logger
from app.db.models import (
    DELIVERY_DETAIL_SEQUENCE,
    ConversationStep,
    LeadStage,
    Outlet,
)
from app.integrations.gallabox.messages import ListRow, ListSection
from app.services.conversation import prompts as p
from app.services.conversation.context import FlowContext
from app.services.conversation.validators import (
    clean_address,
    clean_phone,
    is_skip,
)
from app.services.crm_sync import push_details
from app.services.slots import list_available_slots

log = get_logger(__name__)

MAX_SLOT_ROWS = 10
DETAIL_CURSOR = "detail_index"

PROMPT_FOR_STEP = {
    ConversationStep.AWAIT_ADDRESS: p.ASK_ADDRESS,
    ConversationStep.AWAIT_APARTMENT: p.ASK_APARTMENT,
    ConversationStep.AWAIT_INSTRUCTIONS: p.ASK_INSTRUCTIONS,
    ConversationStep.AWAIT_CONTACT_NUMBER: p.ASK_CONTACT_NUMBER,
}


def start_details(ctx: FlowContext) -> None:
    """Reset the cursor so a new order collects fresh delivery details."""
    ctx.put(**{DETAIL_CURSOR: 0})


async def ask_next_detail(ctx: FlowContext) -> None:
    """Ask the next unanswered question, or move on to slots when done."""
    index = int(ctx.get(DETAIL_CURSOR) or 0)

    if index >= len(DELIVERY_DETAIL_SEQUENCE):
        await show_slots(ctx)
        return

    step = DELIVERY_DETAIL_SEQUENCE[index]
    await ctx.reply_text(PROMPT_FOR_STEP[step])
    ctx.goto(step)


async def _advance_detail(ctx: FlowContext) -> None:
    ctx.put(**{DETAIL_CURSOR: int(ctx.get(DETAIL_CURSOR) or 0) + 1})
    await ask_next_detail(ctx)


async def handle_address(ctx: FlowContext) -> None:
    address = clean_address(ctx.text)
    if address is None:
        await ctx.reply_text(p.ADDRESS_REASK)
        return
    ctx.customer.address_line1 = address
    await _advance_detail(ctx)


async def handle_apartment(ctx: FlowContext) -> None:
    # Optional field - 'skip' is a valid answer, so nothing is re-asked.
    text = ctx.text
    ctx.customer.apartment_unit = None if is_skip(text, p.SKIP_TOKENS) else text[:120]
    await _advance_detail(ctx)


async def handle_instructions(ctx: FlowContext) -> None:
    text = ctx.text
    ctx.customer.delivery_instructions = (
        None if is_skip(text, p.SKIP_TOKENS) else text[:500]
    )
    await _advance_detail(ctx)


async def handle_contact_number(ctx: FlowContext) -> None:
    number = clean_phone(ctx.text, fallback=ctx.customer.whatsapp_number)
    if number is None:
        await ctx.reply_text(p.CONTACT_REASK)
        return

    ctx.customer.contact_number = number
    await push_details(
        ctx.customer,
        address=ctx.customer.address_line1,
        apartment_unit=ctx.customer.apartment_unit,
        postal_code=ctx.customer.postal_code,
    )
    await _advance_detail(ctx)


def missing_details(ctx: FlowContext) -> list[str]:
    """Mandatory fields still blank - payment stays blocked while any remain."""
    missing = []
    if not ctx.customer.address_line1:
        missing.append("delivery address")
    if not (ctx.customer.contact_number or ctx.customer.whatsapp_number):
        missing.append("contact number")
    return missing


# --- slots (step 13) ---------------------------------------------------------
async def show_slots(ctx: FlowContext) -> None:
    """Offer the available delivery slots for the chosen outlet."""
    outlet = await _current_outlet(ctx)
    if outlet is None:
        await ctx.reply_text(p.GENERIC_ERROR)
        return

    slots = await list_available_slots(ctx.session, outlet, limit=MAX_SLOT_ROWS)
    if not slots:
        await ctx.reply_text(p.NO_SLOTS)
        log.warning("no_slots_available", outlet=outlet.code)
        return

    ctx.put(slots=[
        {"slot_id": s.slot_id, "label": s.label,
         "starts_at": s.starts_at.isoformat(), "ends_at": s.ends_at.isoformat()}
        for s in slots
    ])

    rows = [ListRow(id=f"{p.SLOT_PREFIX}{s.slot_id}", title=s.label) for s in slots]
    await ctx.reply_list(
        p.SLOT_PROMPT,
        [ListSection(title="Available slots", rows=rows)],
        button_text=p.SLOT_LIST_BUTTON,
    )
    ctx.goto(ConversationStep.AWAIT_SLOT_CHOICE)


async def handle_slot_choice(ctx: FlowContext) -> None:
    """Customer picked a slot: build the order and show the summary."""
    from app.services.conversation.handlers import checkout

    choice = ctx.choice
    slot_id = choice[len(p.SLOT_PREFIX):] if choice.startswith(p.SLOT_PREFIX) else ""

    chosen = next(
        (s for s in (ctx.get("slots") or []) if s["slot_id"] == slot_id), None
    )
    if chosen is None:
        await ctx.reply_text(p.FALLBACK)
        await show_slots(ctx)
        return

    # Guard the spec's "block payment until all fields are filled".
    still_missing = missing_details(ctx)
    if still_missing:
        log.info("details_incomplete_at_slot", missing=still_missing)
        ctx.put(**{DETAIL_CURSOR: 0})
        await ask_next_detail(ctx)
        return

    ctx.put(slot_id=chosen["slot_id"], slot_label=chosen["label"],
            slot_starts_at=chosen["starts_at"], slot_ends_at=chosen["ends_at"])
    await ctx.set_stage(LeadStage.SLOT_SELECTED)

    try:
        await checkout.build_and_show_summary(ctx)
    except NoSlotsAvailableError as exc:
        # The slot filled between being listed and being picked.
        await ctx.reply_text(exc.customer_message)
        await show_slots(ctx)


async def _current_outlet(ctx: FlowContext) -> Outlet | None:
    """The kitchen whose slots are offered."""
    from app.services import kitchen as kitchen_service

    outlet_id = ctx.get("outlet_id") or ctx.customer.preferred_outlet_id
    if outlet_id:
        try:
            found = await ctx.session.get(Outlet, uuid.UUID(str(outlet_id)))
            if found is not None:
                return found
        except ValueError:
            pass

    kitchen = await kitchen_service.get_kitchen(ctx.session)
    if kitchen is None:
        log.error("no_kitchen_configured", customer_id=str(ctx.customer.id))
    return kitchen
