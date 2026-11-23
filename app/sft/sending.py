"""Sending SFT's messages: on the right number, logged on the lead's timeline.

Every send goes through the current sender (so the admin's test chat can
collect the replies instead of delivering them) on SFT's own channel when it
has one. A send Gallabox refuses is logged and recorded, never raised: the
customer's next message must still be answered.

Inside WhatsApp's 24-hour window (24 hours from their last message) any
message can go; outside it only an approved template.
"""

from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConfigurationError, IntegrationError
from app.core.logging import get_logger
from app.db.models import SftLead
from app.integrations.gallabox import messages as m
from app.integrations.gallabox.client import use_channel
from app.integrations.gallabox.sender import current_sender
from app.integrations.gallabox.templates import TemplateSpec
from app.sft import content as c
from app.sft.config import SftConfig
from app.sft.leads import aware, event, now

log = get_logger(__name__)

WINDOW = timedelta(hours=24)
# A little margin: a message that leaves at 23:59 may arrive after 24:00.
WINDOW_MARGIN = timedelta(minutes=10)
SEND_ERRORS = (IntegrationError, ConfigurationError, ValueError)


def in_window(lead: SftLead, at=None) -> bool:
    """Inside WhatsApp's 24 hours from their last message (at `at`, default now)."""
    last = aware(lead.last_inbound_at)
    return last is not None and (at or now()) - last < WINDOW - WINDOW_MARGIN


def _channel(lead: SftLead, cfg: SftConfig) -> str | None:
    return lead.channel_id or cfg.channel_id or None


async def _go(session: AsyncSession, lead: SftLead, cfg: SftConfig, what: str, send,
              detail: str, extra: dict | None = None) -> bool:
    try:
        with use_channel(_channel(lead, cfg)):
            await send(current_sender())
    except SEND_ERRORS as exc:
        log.error("sft_send_failed", what=what, lead_id=str(lead.id), error=str(exc))
        await event(session, lead, "send_failed", f"{what}: {exc}"[:300], data={"what": what})
        return False
    lead.last_outbound_at = now()
    await event(session, lead, "sent", detail, data={"what": what, **(extra or {})})
    return True


async def text(session, lead, cfg, body: str, *, what: str = "text") -> bool:
    return await _go(session, lead, cfg, what, lambda s: s.send_text(lead.whatsapp_number, body),
                     body)


async def buttons(session, lead, cfg, body: str, choices, *, what: str = "buttons") -> bool:
    rows = [m.Button(id=b.id, title=b.title) for b in choices]
    shown = body + "  [" + " | ".join(b.title for b in choices) + "]"
    return await _go(session, lead, cfg, what,
                     lambda s: s.send_buttons(lead.whatsapp_number, body, rows), shown,
                     {"buttons": [[b.id, b.title] for b in choices]})


async def link(session, lead, cfg, body: str, *, url: str, label: str,
               what: str = "link") -> bool:
    return await _go(session, lead, cfg, what,
                     lambda s: s.send_cta_url(lead.whatsapp_number, body, url=url,
                                              display_text=label),
                     f"{body}  [{label}]", {"link": url, "label": label})


async def faq_list(session, lead, cfg, body: str) -> bool:
    rows = [m.ListRow(id=i, title=t, description=d) for i, t, d in c.FAQ_ROWS]
    section = m.ListSection(title="Questions", rows=rows)
    return await _go(session, lead, cfg, "faq",
                     lambda s: s.send_list(lead.whatsapp_number, body, [section],
                                           button_text=c.FAQ_BUTTON),
                     body + "  [list: " + ", ".join(t for _, t, _ in c.FAQ_ROWS) + "]",
                     {"buttons": [[i, t] for i, t, _ in c.FAQ_ROWS]})


async def document(session, lead, cfg, *, url: str, filename: str, caption: str) -> bool:
    return await _go(session, lead, cfg, "brochure",
                     lambda s: s.send_document(lead.whatsapp_number, url=url, filename=filename,
                                               caption=caption),
                     f"📄 {filename}", {"link": url, "label": "Open the brochure"})


async def template(session, lead, cfg, spec: TemplateSpec, *values: object, at=None) -> str:
    """A follow-up: the template when it is approved; inside the window the
    same words as an ordinary message when it is not. Returns "template",
    "text", or "" when nothing could go (outside the window, not approved)."""
    if await approved(spec.name, _channel(lead, cfg)):
        ok = await _go(session, lead, cfg, spec.name,
                       lambda s: s.send_template_only(lead.whatsapp_number, spec, *values),
                       f"[template {spec.name}] " + _filled(spec, values),
                       {"buttons": [[c.LABELS.get(_norm(q), q), q] for q in spec.quick_replies]})
        return "template" if ok else ""
    if not in_window(lead, at):
        await event(session, lead, "not_sent",
                    f"{spec.name} is not approved on WhatsApp yet and the 24-hour window is "
                    "closed, so it could not go", data={"what": spec.name})
        return ""
    body = _filled(spec, values)
    if spec.quick_replies:
        choices = [c.Button(id=c.LABELS.get(_norm(q), q), title=q)
                   for q in spec.quick_replies[:3]]
        ok = await buttons(session, lead, cfg, body, choices, what=spec.name)
    else:
        ok = await text(session, lead, cfg, body, what=spec.name)
    return "text" if ok else ""


def _filled(spec: TemplateSpec, values) -> str:
    from app.integrations.gallabox.fallback import body_text

    return body_text(spec, tuple(values))


def _norm(label: str) -> str:
    from app.services.reply_triggers import normalise

    return normalise(label)


async def handover(lead: SftLead, cfg: SftConfig, note: str) -> None:
    """Ask Gallabox to put the chat in the team inbox (best effort)."""
    try:
        with use_channel(_channel(lead, cfg)):
            sender = current_sender()
            if hasattr(sender, "handover_to_agent"):
                await sender.handover_to_agent(lead.whatsapp_number, note=note)
    except Exception as exc:  # noqa: BLE001 - the team still sees the chat in Gallabox
        log.warning("sft_handover_failed", lead_id=str(lead.id), error=str(exc))


# --- template approval on SFT's channel ---------------------------------------------
CACHE_SECONDS = 300
_approved: dict[str, tuple[float, dict[str, bool]]] = {}


async def approved(name: str, channel: str | None) -> bool:
    """Approved, on the channel SFT sends from, long enough ago to use."""
    from app.integrations.gallabox import template_admin, template_status

    if not channel:
        # Shero US's own number: the bot's usual check.
        return await template_status.is_approved(name)
    cached = _approved.get(channel)
    if cached is None or time.monotonic() - cached[0] > CACHE_SECONDS:
        try:
            rows = await template_admin.list_templates(channel_id=channel)
        except Exception as exc:  # noqa: BLE001 - unknown means not usable
            log.warning("sft_template_status_unavailable", error=str(exc))
            return False
        clock = await template_status.gallabox_now()
        cached = (time.monotonic(),
                  {str(r.get("name")): template_status.usable(r, clock) for r in rows})
        _approved[channel] = cached
    return cached[1].get(name, False)


def clear_cache() -> None:
    _approved.clear()
