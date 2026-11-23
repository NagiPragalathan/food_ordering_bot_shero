"""Gallabox WhatsApp client.

Auth is a static apiKey/apiSecret header pair (Gallabox > Settings > API Keys).

Endpoint shapes below match Gallabox's public dev API. They are isolated in
this one module on purpose: if the client's Gallabox plan exposes a different
route, only this file changes - the bot engine talks in terms of
`send_text` / `send_buttons` / `send_template`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from app.core.config import settings
from app.core.exceptions import IntegrationError
from app.core.logging import get_logger
from app.integrations.base import ApiClient
from app.integrations.gallabox import messages as m
from app.integrations.gallabox.templates import TemplateSpec, render

log = get_logger(__name__)

# The channel (WhatsApp number) to send from instead of the business's own,
# for the rest of this task: Shero Food Training may have a number of its
# own in the same Gallabox account (app/sft/sending.py).
_channel: ContextVar[str | None] = ContextVar("gallabox_channel", default=None)


def channel_id() -> str:
    """The channel sends go out on: an override, or the business's own."""
    return _channel.get() or settings.gallabox_channel_id


@contextmanager
def use_channel(channel: str | None) -> Iterator[None]:
    """Send from `channel` inside this block (None or "": the business's own)."""
    token = _channel.set(channel or None)
    try:
        yield
    finally:
        _channel.reset(token)


class GallaboxClient(ApiClient):
    service = "gallabox"

    def __init__(self) -> None:
        super().__init__(base_url=settings.gallabox_base_url)

    async def default_headers(self) -> dict[str, str]:
        return {
            "apiKey": settings.gallabox_api_key,
            "apiSecret": settings.gallabox_api_secret,
            "Content-Type": "application/json",
        }

    # -- core send ------------------------------------------------------------
    async def _send(self, to: str, whatsapp_block: dict, *, name: str | None = None) -> dict:
        payload = {
            "channelId": channel_id(),
            "channelType": "whatsapp",
            "recipient": {"phone": normalise_phone(to), **({"name": name} if name else {})},
            "whatsapp": whatsapp_block,
        }
        result = await self.post("/messages/whatsapp", json=payload)
        log.info("whatsapp_sent", to=mask_phone(to), kind=whatsapp_block.get("type"))
        return result or {}

    # -- convenience wrappers used by the conversation engine -----------------
    async def send_text(self, to: str, body: str, *, name: str | None = None) -> dict:
        return await self._send(to, m.text_message(body), name=name)

    async def send_buttons(self, to: str, body: str, buttons: list[m.Button], *,
                           header: str | None = None, footer: str | None = None) -> dict:
        return await self._send(to, m.button_message(body, buttons, header=header, footer=footer))

    async def send_list(self, to: str, body: str, sections: list[m.ListSection], *,
                        button_text: str = "Choose", header: str | None = None,
                        footer: str | None = None) -> dict:
        return await self._send(
            to, m.list_message(body, sections, button_text=button_text,
                               header=header, footer=footer)
        )

    async def send_cta_url(self, to: str, body: str, *, url: str,
                           display_text: str, header: str | None = None,
                           footer: str | None = None) -> dict:
        return await self._send(to, m.cta_url_message(
            body, url=url, display_text=display_text,
            header=header, footer=footer))

    async def send_document(self, to: str, *, url: str, filename: str,
                            caption: str | None = None) -> dict:
        """A file (the SFT brochure PDF) the customer taps to open.

        WhatsApp fetches `url` itself when it delivers the message, so it
        must be public."""
        return await self._send(to, m.document_message(url, filename=filename, caption=caption))

    async def request_location(self, to: str, body: str) -> dict:
        return await self._send(to, m.location_request_message(body))

    async def send_product_list(self, to: str, sections: list[dict], *, header: str,
                                body: str, footer: str | None = None) -> dict:
        return await self._send(
            to,
            m.product_list_message(settings.meta_catalog_id, sections,
                                   header=header, body=body, footer=footer),
        )

    async def send_template(self, to: str, spec: TemplateSpec, *values: object,
                            button_value: str | None = None) -> dict:
        """Send an approved template; otherwise the same message as ordinary
        text (see fallback.py), so the customer always gets it.

        Gallabox accepts a send for an unapproved template and drops it
        silently, so approval is checked before sending rather than inferred
        from an error. A template Gallabox refuses outright falls back too.
        """
        # Imported here: template_status reads the template list through
        # this client, so importing it at module level would be circular.
        from app.integrations.gallabox import fallback, template_status

        payload = render(spec, *values, button_value=button_value)  # validates the values
        if await template_status.is_approved(spec.name):
            try:
                return await self._send(to, payload)
            except IntegrationError as exc:
                log.error("template_send_failed_using_text", template=spec.name,
                          error=str(exc))
        else:
            log.info("template_not_approved_using_text", template=spec.name)
        return await self._send(to, fallback.message(spec, tuple(values), button_value))

    async def send_template_only(self, to: str, spec: TemplateSpec, *values: object,
                                 button_value: str | None = None) -> dict:
        """Send the template and nothing else: no text fallback.

        For messages that go outside the 24-hour window (services/nudges),
        where WhatsApp drops ordinary text, so a fallback would only look
        like it worked. The caller checks approval first; a refusal raises.
        """
        return await self._send(to, render(spec, *values, button_value=button_value))

    # -- agent handover (spec step 5, "Talk to Us") ---------------------------
    async def handover_to_agent(self, to: str, *, note: str | None = None) -> dict:
        """Take the bot out of the loop so a human agent can take over.

        Gallabox models this as assigning the contact's conversation to the
        team inbox. If the client's workspace uses a different mechanism
        (a bot-flow "handoff" node, say), swap the call here.
        """
        payload = {
            "channelId": channel_id(),
            "phone": normalise_phone(to),
            "assignedTo": None,      # null -> unassigned team inbox
            "botEnabled": False,     # stop the bot replying over the agent
        }
        if note:
            payload["note"] = note
        try:
            return await self.post("/conversations/assign", json=payload) or {}
        except Exception as exc:  # noqa: BLE001 - handover must never break the chat
            # The customer has already been told an agent is coming; log loudly
            # so ops can pick it up manually rather than dropping the thread.
            log.error("handover_failed", to=mask_phone(to), error=str(exc))
            return {}


def normalise_phone(raw: str) -> str:
    """Strip to digits only - the format Gallabox/WhatsApp expect (E.164, no +)."""
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def mask_phone(raw: str) -> str:
    """Log-safe phone: keep the last 4 digits only."""
    digits = normalise_phone(raw)
    return f"***{digits[-4:]}" if len(digits) >= 4 else "***"


gallabox = GallaboxClient()
