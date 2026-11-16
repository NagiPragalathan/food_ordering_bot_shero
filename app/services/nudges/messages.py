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
from app.core import tenants
from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Customer
from app.integrations.gallabox import template_admin, template_status
from app.integrations.gallabox.templates import BY_NAME, TemplateSpec, catalogue_name
from app.services.nudges.config import Message

log = get_logger(__name__)

# Shero US's messages. Template names are unique across the Gallabox
# account, so each business names its own: shero_nudge_... for Shero US,
# dst_nudge_... for DST India (`prefix()`).
PREFIX = "shero_nudge_"
# Approved templates that can be added to the list as they are: for Shero US
# the 54 SMO follow-ups and messages once made here (`addable_prefixes()`).
ADDABLE = ("smo_", PREFIX)
# "Hi {{1}}," - a template whose one variable is the customer's first name,
# right after the greeting (DST India's F1-F12 all start this way).
GREETING_NAME = re.compile(r"^(?:Hi|Hello|Hey|Dear)\s+\{\{1\}\}")
STATUS_SECONDS = 60
# Gallabox's words for a template's state -> the page's.
STATES = {"approved": "approved", "pending": "pending", "in_review": "pending",
          "submitted": "pending", "rejected": "rejected", "error": "rejected",
          "paused": "paused", "disabled": "rejected"}

# Per business: each has its own WhatsApp number and templates.
_caches = tenants.PerTenant(lambda: {"at": 0.0, "rows": None})


def prefix() -> str:
    """This business's nudge template names: shero_nudge_ (Shero US),
    dst_nudge_ (DST India)."""
    own = settings.template_prefix
    return f"{own}nudge_" if own else PREFIX


def addable_prefixes() -> tuple[str, ...]:
    """Which approved templates on this business's number can join the list:
    Shero US's SMO follow-ups and its own messages; for DST India everything
    named dst_ - its F1-F12 follow-ups and its own dst_nudge_ messages."""
    own = settings.template_prefix
    return (own, prefix()) if own else ADDABLE


def name_for(label: str, taken: set[str]) -> str:
    """A template name from what the team calls the message: "Cart reminder"
    -> shero_nudge_cart_reminder (dst_nudge_cart_reminder for DST India), with
    _2, _3... when that is taken."""
    slug = re.sub(r"[^a-z0-9]+", "_", (label or "").lower()).strip("_")[:60] or "message"
    start = prefix()
    name, n = f"{start}{slug}", 2
    while name in taken:
        name, n = f"{start}{slug}_{n}", n + 1
    return name


def spec(message: Message) -> TemplateSpec:
    has_name = "{name}" in message.text
    return TemplateSpec(
        name=message.name, category="MARKETING", trigger="nudge",
        params=("customer_name",) if has_name else (),
        sample_body=message.text.replace("{name}", "{{1}}"),
        button_kind="quick_reply" if message.button_labels else "none",
        quick_replies=message.button_labels)


def values(message: Message, customer: Customer) -> tuple[str, ...]:
    if "{name}" not in message.text:
        return ()
    first = (customer.name or "").strip().split(" ")[0]
    return (first or "there",)


def from_template(row: dict) -> dict | None:
    """A message from an approved template on Gallabox, or None when it can't
    be sent as it is: a variable or a link button needs values the nudges
    don't have."""
    if str(row.get("status") or "").lower() != "approved" or row.get("category") != "MARKETING":
        return None
    body, buttons = "", []
    for part in row.get("components") or []:
        if part.get("type") == "BODY":
            body = str(part.get("text") or "")
        elif part.get("type") in ("HEADER", "FOOTER") and "{{" in str(part.get("text") or ""):
            return None
        elif part.get("type") == "HEADER" and part.get("format") not in (None, "TEXT"):
            return None
        elif part.get("type") == "BUTTONS":
            for button in part.get("buttons") or []:
                if button.get("type") != "QUICK_REPLY":
                    return None
                buttons.append(str(button.get("text") or ""))
    body = body.strip()
    if "{{" in body:
        body = _with_name(body)
        if body is None:
            return None
    if not body:
        return None
    return {"name": row["name"], "text": body, "buttons": buttons, "source": "whatsapp"}


def _with_name(body: str) -> str | None:
    """A body whose only variable is the first name after the greeting ("Hi
    {{1}}, ..."), written as a message's {name}; None for any other variable.

    Only for a business whose own templates are known to be written that way
    (DST India's, prefixed): Shero US's list stays exactly as it was."""
    if not settings.template_prefix or not GREETING_NAME.match(body):
        return None
    if set(re.findall(r"\{\{(\d+)\}\}", body)) != {"1"} or body.count("{{1}}") != 1:
        return None
    if re.search(r"\{\{(?!1\}\})", body):
        return None
    return body.replace("{{1}}", "{name}")


async def addable(taken: set[str]) -> list[dict] | None:
    """Approved templates that can join the list, not in it yet (None: Gallabox
    couldn't be asked)."""
    if await statuses() is None:
        return None
    wanted = addable_prefixes()
    found = (from_template(r) for r in _caches.get()["rows"] or []
             if str(r.get("name", "")).startswith(wanted) and r.get("name") not in taken
             # The ordering catalogue's own copies (payment, feedback...) are
             # not nudges, whatever their prefix.
             and catalogue_name(str(r.get("name"))) not in BY_NAME)
    return sorted((m for m in found if m), key=lambda m: m["name"])


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
    if fresh or _caches.get()["rows"] is None or time.monotonic() - _caches.get()["at"] > STATUS_SECONDS:
        try:
            rows = await template_admin.list_templates()
        except (IntegrationError, ConfigurationError) as exc:
            log.warning("nudge_template_list_failed", error=str(exc))
            return None
        _caches.get().update(at=time.monotonic(), rows=rows)
    return {str(r.get("name")): STATES.get(str(r.get("status") or "").lower(), "pending")
            for r in _caches.get()["rows"] or [] if r.get("name")}


async def approved(name: str) -> bool:
    """Ready to send: approved, and settled on WhatsApp's side."""
    return await template_status.is_approved(name)


def clear_cache() -> None:
    _caches.get().update(at=0.0, rows=None)
    template_status.clear_cache()
