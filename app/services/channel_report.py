"""Who the bot recorded from other teams' WhatsApp numbers. Reads only.

Before integrations/gallabox/channel.py, the webhook answered every channel in
the Gallabox account, so other teams' customers got a customer record (and
often a Zoho Lead) here. This tells them apart from the bot's own customers by
the channel stored on each logged inbound message.

A customer is listed when every message logged from them came in on another
channel. Anyone who also wrote to the bot's own number is not.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Customer, InboundMessage, Order
from app.integrations.gallabox import channel
from app.integrations.gallabox.client import normalise_phone
from app.schemas.inbound import parse_inbound


@dataclass
class Found:
    customer: Customer
    channels: set[str] = field(default_factory=set)     # the other channel numbers
    messages: int = 0
    orders: int = 0

    @property
    def zoho_id(self) -> str:
        return self.customer.zoho_contact_id or self.customer.zoho_lead_id or ""


@dataclass
class Report:
    customers: list[Found]
    other_messages: int          # logged messages from other channels, anyone's


async def build(session: AsyncSession) -> Report:
    ours: set[str] = set()
    theirs: dict[str, tuple[int, set[str]]] = defaultdict(lambda: (0, set()))
    other_messages = 0
    for number, payload in (await session.execute(
            select(InboundMessage.whatsapp_number, InboundMessage.payload))).all():
        event = parse_inbound(payload or {})
        if not (event.channel_id or event.channel_number):
            continue                    # nothing to place it by
        key = normalise_phone(number or event.whatsapp_number)
        if channel.is_ours(event):
            ours.add(key)
            continue
        other_messages += 1
        count, numbers = theirs[key]
        theirs[key] = (count + 1, numbers | {event.channel_number or event.channel_id})

    found = []
    for key in set(theirs) - ours:
        customer = (await session.execute(
            select(Customer).where(Customer.whatsapp_number == key))).scalar_one_or_none()
        if customer is None:
            continue
        orders = (await session.execute(select(func.count(Order.id))
                                        .where(Order.customer_id == customer.id))).scalar_one()
        count, numbers = theirs[key]
        found.append(Found(customer, numbers, count, orders))
    found.sort(key=lambda f: str(f.customer.created_at or ""))
    return Report(found, other_messages)
