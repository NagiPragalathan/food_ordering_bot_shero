"""Shared bits of the SFT tests: a recorder in place of Gallabox, a lead's
messages, and SFT switched on."""

from __future__ import annotations

import itertools

import pytest
import pytest_asyncio

from app.integrations.gallabox.sender import use_sender
from app.schemas.inbound import InboundEvent, InboundKind
from app.sft import config, flow

NUMBER = "15551230001"
_ids = itertools.count()


class Recorder:
    """What SFT would have sent on WhatsApp, as (kind, body, extra)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, object]] = []

    async def send_text(self, to, body, name=None):
        self.sent.append(("text", body, None))

    async def send_buttons(self, to, body, buttons, header=None, footer=None):
        self.sent.append(("buttons", body, [(b.id, b.title) for b in buttons]))

    async def send_list(self, to, body, sections, button_text="Choose", header=None,
                        footer=None):
        self.sent.append(("list", body, [(r.id, r.title) for s in sections for r in s.rows]))

    async def send_cta_url(self, to, body, *, url, display_text, header=None, footer=None):
        self.sent.append(("link", body, (url, display_text)))

    async def send_document(self, to, *, url, filename, caption=None):
        self.sent.append(("document", filename, url))

    async def send_template_only(self, to, spec, *values, button_value=None):
        self.sent.append(("template", spec.name, values))

    async def send_template(self, to, spec, *values, button_value=None):
        self.sent.append(("alert", to, (spec.name, values)))

    async def handover_to_agent(self, to, note=None):
        self.sent.append(("handover", note or "", None))

    def kinds(self) -> list[str]:
        return [k for k, _, _ in self.sent]

    def last(self):
        assert self.sent, "nothing was sent"
        return self.sent[-1]

    def buttons(self) -> list[str]:
        """The titles of the last message's buttons (or list rows)."""
        for kind, _, extra in reversed(self.sent):
            if kind in ("buttons", "list"):
                return [title for _, title in extra]
        return []

    def clear(self) -> None:
        self.sent.clear()


@pytest.fixture
def rec():
    recorder = Recorder()
    with use_sender(recorder):
        yield recorder


@pytest_asyncio.fixture
async def cfg(session):
    """SFT live on Shero US's number, defaults otherwise."""
    return await config.save(session, {"mode": "live"}, "test@shero")


def event(text: str = "", *, button: str = "", title: str = "", number: str = NUMBER,
          name: str = "priya raman", kind: InboundKind | None = None, **extra) -> InboundEvent:
    return InboundEvent(whatsapp_number=number,
                        kind=kind or (InboundKind.REPLY if button else InboundKind.TEXT),
                        text=title if button else text, reply_id=button, reply_title=title,
                        contact_name=name, message_id=f"m{next(_ids)}", **extra)


async def say(session, cfg, text: str = "", **kw) -> str:
    """One message from the lead, through the flow."""
    return await flow.handle(session, event(text, **kw), cfg)
