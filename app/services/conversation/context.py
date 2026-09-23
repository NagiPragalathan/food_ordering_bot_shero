"""The per-message context handed to every step handler.

Bundles the database session, the customer, their conversation row and the
parsed inbound event, and exposes small `reply_*` helpers so handlers describe
what to say rather than how to send it. Swapping the messaging provider means
changing this file, not twenty handlers.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Conversation, ConversationStep, Customer, LeadStage
from app.integrations.gallabox import messages as m
from app.integrations.gallabox.sender import current_sender
from app.schemas.inbound import InboundEvent
from app.services.crm_sync import advance_stage
from app.services.customers import advance

log = get_logger(__name__)


@dataclass
class FlowContext:
    session: AsyncSession
    customer: Customer
    conversation: Conversation
    event: InboundEvent

    # --- outbound ------------------------------------------------------------
    async def reply_text(self, body: str) -> None:
        await current_sender().send_text(self.customer.whatsapp_number, body,
                                 name=self.customer.name)

    async def reply_buttons(self, body: str, buttons: list[m.Button], *,
                            header: str | None = None,
                            footer: str | None = None) -> None:
        await current_sender().send_buttons(self.customer.whatsapp_number, body, buttons,
                                    header=header, footer=footer)

    async def reply_list(self, body: str, sections: list[m.ListSection], *,
                         button_text: str = "Choose",
                         header: str | None = None) -> None:
        await current_sender().send_list(self.customer.whatsapp_number, body, sections,
                                 button_text=button_text, header=header)

    async def request_location(self, body: str) -> None:
        await current_sender().request_location(self.customer.whatsapp_number, body)

    async def reply_products(self, sections: list[dict], *, header: str,
                             body: str, footer: str | None = None) -> None:
        await current_sender().send_product_list(self.customer.whatsapp_number, sections,
                                         header=header, body=body, footer=footer)

    async def handover(self, note: str | None = None) -> None:
        await current_sender().handover_to_agent(self.customer.whatsapp_number, note=note)

    # --- state ---------------------------------------------------------------
    def goto(self, step: ConversationStep) -> None:
        advance(self.conversation, step)

    async def set_stage(self, stage: LeadStage) -> None:
        """Record the lead stage locally and mirror it to Zoho."""
        await advance_stage(self.customer, stage)

    # --- convenience ---------------------------------------------------------
    @property
    def step(self) -> str:
        return self.conversation.step

    @property
    def text(self) -> str:
        return self.event.text.strip()

    @property
    def choice(self) -> str:
        return self.event.choice

    def get(self, key: str, default=None):
        return self.conversation.get(key, default)

    def put(self, **values) -> None:
        self.conversation.set(**values)

    def drop(self, *keys: str) -> None:
        self.conversation.clear(*keys)
