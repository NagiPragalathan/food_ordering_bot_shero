"""Location and serviceability (steps 9-10).

There is one kitchen, so the spec's step 11 ("pick a nearby outlet") is gone.
What remains is the question that actually matters: do we deliver here?
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.db.models import ConversationStep, LeadStage
from app.services import kitchen as kitchen_service
from app.services.conversation import prompts as p
from app.services.conversation.context import FlowContext
from app.services.conversation.validators import clean_postal_code
from app.services.crm_sync import push_details

log = get_logger(__name__)


async def request_or_resume(ctx: FlowContext) -> None:
    """Step 9: ask for a location, unless Check Availability already got one."""
    if ctx.customer.latitude is not None and ctx.customer.longitude is not None:
        log.info("location_reused_from_availability_check",
                 customer_id=str(ctx.customer.id))
        await confirm_service(ctx)
        return

    await ctx.request_location(p.ASK_LOCATION)
    ctx.goto(ConversationStep.AWAIT_LOCATION)


async def handle_location(ctx: FlowContext) -> None:
    """Parse the pin or ZIP, re-asking if it cannot be read."""
    postal_code = clean_postal_code(ctx.text)
    point = await kitchen_service.resolve_location(
        latitude=ctx.event.latitude,
        longitude=ctx.event.longitude,
        postal_code=postal_code,
    )
    if point is None:
        await ctx.reply_text(p.LOCATION_UNREADABLE)
        return

    ctx.customer.latitude = point.latitude
    ctx.customer.longitude = point.longitude
    if point.postal_code:
        ctx.customer.postal_code = point.postal_code
    ctx.put(postal_code=point.postal_code, city=point.city, state=point.state)

    await confirm_service(ctx)


async def confirm_service(ctx: FlowContext) -> None:
    """Step 10: serviceable or not. Serviceable moves straight to step 12."""
    from app.services.conversation.handlers import delivery

    point = await kitchen_service.resolve_location(
        latitude=ctx.customer.latitude, longitude=ctx.customer.longitude
    )
    check = await kitchen_service.check_service(
        ctx.session, point, postal_code=ctx.customer.postal_code
    )

    if not check.is_serviceable:
        log.info("not_serviceable", reason=check.reason)
        await ctx.reply_text(p.NOT_SERVICEABLE)
        await ctx.set_stage(LeadStage.NOT_SERVICEABLE)
        ctx.goto(ConversationStep.COMPLETED)
        return

    kitchen = await kitchen_service.get_kitchen(ctx.session)
    if kitchen is None:
        await ctx.reply_text(p.NO_KITCHEN)
        ctx.goto(ConversationStep.COMPLETED)
        return

    ctx.customer.preferred_outlet_id = kitchen.id
    if check.distance_km is not None:
        ctx.customer.distance_km = check.distance_km
    ctx.put(outlet_id=str(kitchen.id), outlet_name=kitchen.name,
            distance_km=check.distance_km)

    # The spec's "Outlet Selected" stage still marks the point where the
    # kitchen is locked in, even though there is only one to choose from.
    await ctx.set_stage(LeadStage.OUTLET_SELECTED)
    await push_details(ctx.customer, outlet_name=kitchen.name,
                       distance_km=check.distance_km)

    await ctx.reply_text(p.SERVICEABLE_CONFIRMED)

    # Reset the question cursor so every order collects fresh details.
    delivery.start_details(ctx)
    await delivery.ask_next_detail(ctx)
