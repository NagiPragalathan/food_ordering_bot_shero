"""The order summary's Change menu and Update location buttons.

Both release the unpaid order (slot hold and Pay Now link), put its dishes
back in the cart, and send a fresh link. Update location's link opens the
address-only page (order/location.html) rather than the menu.
"""

from __future__ import annotations

from app.core.config import settings
from app.core.logging import get_logger
from app.services import order_changes
from app.services.conversation import prompts as p
from app.services.conversation.context import FlowContext

log = get_logger(__name__)


async def change_menu(ctx: FlowContext) -> None:
    await _reopen_and_link(ctx, reason="change_menu", body=p.CHANGE_MENU_BODY)


async def update_location(ctx: FlowContext) -> None:
    await _reopen_and_link(ctx, reason="update_location", body=p.UPDATE_LOCATION_BODY,
                           open_at="address", button=p.UPDATE_LOCATION_BUTTON)


async def _reopen_and_link(ctx: FlowContext, *, reason: str, body: str,
                           open_at: str | None = None,
                           button: str | None = None) -> None:
    from app.services.conversation.handlers import menu

    result = await order_changes.reopen_latest(ctx.session, ctx.customer,
                                               ctx.conversation, reason=reason)
    if result.already_paid:
        await ctx.reply_text(p.ORDER_ALREADY_PAID.format(order=result.order_number))
        return

    released = (p.ORDER_RELEASED.format(order=result.order_number)
                if result.order_number else "")

    if settings.order_mode == "web":
        await menu.send_order_link(ctx, body=f"{released} {body}".strip(),
                                   open_at=open_at, button=button)
        return

    # Chat browsing has no page to open; the cart review leads on to
    # checkout, which asks for the location again.
    if released:
        await ctx.reply_text(released)
    await menu.show_cart(ctx)
