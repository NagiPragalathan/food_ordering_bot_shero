"""The cart, stored on the conversation and shared by WhatsApp and the web.

One customer has one cart. Adding a dish on the ordering page and adding one
in chat write to the same `conversations.context["cart"]` list, so closing the
page and coming back - or switching between the two - never loses anything.

A line is `{"retailer_id", "name", "quantity", "unit_price"}`. `unit_price` is
a display convenience only: checkout re-reads every price from the menu, so a
stale value here can never become what a customer is charged.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Conversation, Customer
from app.services.customers import get_or_create_conversation

MAX_QUANTITY = 20


def read(conversation: Conversation) -> list[dict]:
    """The cart as a list of plain dicts, never the stored object itself."""
    return [dict(line) for line in (conversation.get("cart") or [])]


def write(conversation: Conversation, lines: list[dict]) -> None:
    """Replace the cart. Empty lines are dropped rather than stored as zeroes."""
    conversation.set(cart=[line for line in lines if line.get("quantity", 0) > 0])


def add(conversation: Conversation, *, retailer_id: str, name: str,
        price: Decimal | str, quantity: int) -> list[dict]:
    """Add to an existing line for the same dish, or start a new one."""
    lines = read(conversation)

    for line in lines:
        if line.get("retailer_id") == retailer_id:
            line["quantity"] = min(line.get("quantity", 0) + quantity, MAX_QUANTITY)
            break
    else:
        lines.append({
            "retailer_id": retailer_id,
            "name": name,
            "quantity": min(quantity, MAX_QUANTITY),
            "unit_price": str(price),
        })

    write(conversation, lines)
    return read(conversation)


def set_quantity(conversation: Conversation, *, retailer_id: str, name: str,
                 price: Decimal | str, quantity: int) -> list[dict]:
    """Set a line to an exact quantity; 0 removes it.

    This is what the web page's + and - buttons call, so it must be
    idempotent: pressing + twice quickly with the same target quantity leaves
    one line at that quantity, not two presses' worth.
    """
    quantity = max(0, min(int(quantity), MAX_QUANTITY))
    lines = [line for line in read(conversation)
             if line.get("retailer_id") != retailer_id]

    if quantity > 0:
        lines.append({
            "retailer_id": retailer_id,
            "name": name,
            "quantity": quantity,
            "unit_price": str(price),
        })

    write(conversation, lines)
    return read(conversation)


def clear(conversation: Conversation) -> None:
    conversation.set(cart=[])


def item_count(lines: list[dict]) -> int:
    return sum(int(line.get("quantity") or 0) for line in lines)


def subtotal(lines: list[dict]) -> Decimal:
    """Indicative total for the cart badge - not what the customer is charged."""
    total = Decimal("0.00")
    for line in lines:
        try:
            total += Decimal(str(line.get("unit_price") or "0")) * int(
                line.get("quantity") or 0)
        except (ValueError, ArithmeticError):
            continue
    return total


async def for_customer(session: AsyncSession,
                       customer: Customer) -> tuple[Conversation, list[dict]]:
    """The customer's conversation and its cart, creating the row if needed."""
    conversation = await get_or_create_conversation(session, customer)
    return conversation, read(conversation)
