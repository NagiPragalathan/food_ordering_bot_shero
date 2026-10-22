"""Engagement on a customer's Zoho record: a timeline note and tags.

Each WhatsApp nudge is written onto the customer's Lead or Contact, so the
team sees in Zoho who was reminded, about what, and with which coupon - and
can build their own Zoho views and workflows on the tags ("Nudge: Abandoned
cart", "WhatsApp Opt-out").

Zoho creates a tag the first time it is used, so nothing has to be set up.
"""

from __future__ import annotations

from typing import Any

from app.core.exceptions import IntegrationError
from app.core.logging import get_logger
from app.integrations.zoho.client import API_VERSION, zoho_client

log = get_logger(__name__)

NOTE_TITLE_MAX = 120
NOTE_MAX = 30000
TAG_MAX = 50


def _record_path(module: str, record_id: str) -> str:
    return f"/crm/{API_VERSION}/{module}/{record_id}"


def _raise_on_refusal(result: Any, what: str) -> None:
    """Zoho answers 200/201 and reports a refused record inside the body."""
    for item in (result or {}).get("data") or []:
        if isinstance(item, dict) and str(item.get("status", "")).lower() == "error":
            raise IntegrationError("zoho", f"{what} refused: {item.get('message') or item}",
                                   payload=item)


async def add_note(module: str, record_id: str, *, title: str, content: str) -> None:
    result = await zoho_client.post(
        f"{_record_path(module, record_id)}/Notes",
        json={"data": [{"Note_Title": title[:NOTE_TITLE_MAX],
                        "Note_Content": content[:NOTE_MAX]}]})
    _raise_on_refusal(result, "note")
    log.info("zoho_note_added", module=module, zoho_id=record_id)


async def add_tags(module: str, record_id: str, tags: list[str]) -> None:
    names = [t.strip()[:TAG_MAX] for t in tags if t and t.strip()]
    if not names:
        return
    result = await zoho_client.post(
        f"{_record_path(module, record_id)}/actions/add_tags",
        json={"tags": [{"name": name} for name in names]})
    _raise_on_refusal(result, "tags")
    log.info("zoho_tags_added", module=module, zoho_id=record_id, tags=names)
