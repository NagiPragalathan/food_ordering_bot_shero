"""Post-delivery rating (step 19)."""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.logging import get_logger
from app.db.models import ConversationStep
from app.services.conversation import prompts as p
from app.services.conversation.context import FlowContext
from app.services.orders import get_latest_for_customer

log = get_logger(__name__)

# The feedback_request template offers exactly these three quick replies.
VALID_RATINGS = {"great": "Great", "good": "Good", "poor": "Poor"}


async def handle_feedback(ctx: FlowContext) -> None:
    """Record the rating against the customer's most recent order."""
    rating = _extract_rating(ctx.choice)
    if rating is None:
        await ctx.reply_text(p.FALLBACK)
        return

    order = await get_latest_for_customer(ctx.session, ctx.customer.id)
    if order is not None:
        order.feedback_rating = rating
        order.feedback_received_at = datetime.now(timezone.utc)
        await ctx.session.flush()
        log.info("feedback_recorded", order_number=order.order_number, rating=rating)
    else:
        log.warning("feedback_without_order", customer_id=str(ctx.customer.id))

    await ctx.reply_text(p.FEEDBACK_THANKS)
    ctx.goto(ConversationStep.COMPLETED)


def _extract_rating(choice: str) -> str | None:
    """Accept the quick-reply payload or the plain word the customer typed."""
    value = (choice or "").strip().lower()
    if value.startswith(p.RATING_PREFIX):
        value = value[len(p.RATING_PREFIX):]
    return VALID_RATINGS.get(value)
