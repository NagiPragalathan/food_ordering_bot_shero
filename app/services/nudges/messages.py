"""The nudges' messages as WhatsApp templates.

A nudge goes outside the 24-hour window, where WhatsApp takes only approved
templates, so each message is submitted to Meta (through Gallabox) when it is
saved, and a rule sends it only once it is approved. Every message has the
same three buttons: Order Food starts an order (it is the bot's ORDER FOOD
keyword), Later and No move the customer to those buckets
(services/nudges/replies.py).

Gallabox cannot edit or delete a template, so a message's text is fixed once
saved: a change is a new message.
"""

from __future__ import annotations

import re
import time

from app.core.exceptions import ConfigurationError, IntegrationError
from app.core.logging import get_logger
from app.db.models import Customer
from app.integrations.gallabox import template_admin, template_status
from app.integrations.gallabox.templates import TemplateSpec
from app.services.nudges.config import Message

log = get_logger(__name__)

BUTTONS = ("Order Food", "Later", "No")
PREFIX = "shero_nudge_"
STATUS_SECONDS = 60
# Gallabox's words for a template's state -> the page's.
STATES = {"approved": "approved", "pending": "pending", "in_review": "pending",
          "submitted": "pending", "rejected": "rejected", "error": "rejected",
          "paused": "paused", "disabled": "rejected"}

_cache: dict = {"at": 0.0, "rows": None}


def name_for(label: str, taken: set[str]) -> str:
    """A template name from what the team calls the message: "Cart reminder"
    -> shero_nudge_cart_reminder, with _2, _3... when that is taken."""
    slug = re.sub(r"[^a-z0-9]+", "_", (label or "").lower()).strip("_")[:60] or "message"
    name, n = f"{PREFIX}{slug}", 2
    while name in taken:
        name, n = f"{PREFIX}{slug}_{n}", n + 1
    return name


def spec(message: Message) -> TemplateSpec:
    has_name = "{name}" in message.text
    return TemplateSpec(
        name=message.name, category="MARKETING", trigger="nudge",
        params=("customer_name",) if has_name else (),
        sample_body=message.text.replace("{name}", "{{1}}"),
        button_kind="quick_reply", quick_replies=BUTTONS)


def values(message: Message, customer: Customer) -> tuple[str, ...]:
    if "{name}" not in message.text:
        return ()
    first = (customer.name or "").strip().split(" ")[0]
    return (first or "there",)


def preview(message: Message) -> str:
    return message.text.replace("{name}", "Asha")


async def submit(message: Message) -> str:
    """Create the template on Gallabox for Meta's approval; its status."""
    record = await template_admin.create_template(spec(message))
    clear_cache()
    return STATES.get(str(record.get("status") or "pending").lower(), "pending")


async def statuses(*, fresh: bool = False) -> dict[str, str] | None:
    """name -> approved | pending | rejected | paused, or None when Gallabox
    cannot be asked. A template not on Gallabox is missing from the map."""
    if fresh or _cache["rows"] is None or time.monotonic() - _cache["at"] > STATUS_SECONDS:
        try:
            rows = await template_admin.list_templates()
        except (IntegrationError, ConfigurationError) as exc:
            log.warning("nudge_template_list_failed", error=str(exc))
            return None
        _cache.update(at=time.monotonic(), rows=rows)
    return {str(r.get("name")): STATES.get(str(r.get("status") or "").lower(), "pending")
            for r in _cache["rows"] or [] if r.get("name")}


async def approved(name: str) -> bool:
    """Ready to send: approved, and settled on WhatsApp's side."""
    return await template_status.is_approved(name)


def clear_cache() -> None:
    _cache.update(at=0.0, rows=None)
    template_status.clear_cache()
