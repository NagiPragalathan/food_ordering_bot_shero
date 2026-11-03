"""A timeline note on a customer's Zoho record.

On a Zoho plan without the Orders module, each order is a note on the
customer's Lead or Contact (services/crm_sync.py).
"""

from __future__ import annotations

from typing import Any

from app.core.exceptions import IntegrationError
from app.core.logging import get_logger
from app.integrations.zoho.client import API_VERSION, zoho_client

log = get_logger(__name__)

NOTE_TITLE_MAX = 120
NOTE_MAX = 30000


def _record_path(module: str, record_id: str) -> str:
    return f"/crm/{API_VERSION}/{module}/{record_id}"


def _raise_on_refusal(result: Any, what: str) -> None:
    """Zoho answers 200/201 and reports a refused record inside the body."""
    for item in (result or {}).get("data") or []:
        if isinstance(item, dict) and str(item.get("status", "")).lower() == "error":
            raise IntegrationError("zoho", f"{what} refused: {item.get('message') or item}",
                                   payload=item)


async def add_note(module: str, record_id: str, *, title: str, content: str) -> str | None:
    """Add a note to the record; returns the note's id."""
    result = await zoho_client.post(
        f"{_record_path(module, record_id)}/Notes",
        json={"data": [{"Note_Title": title[:NOTE_TITLE_MAX],
                        "Note_Content": content[:NOTE_MAX]}]})
    _raise_on_refusal(result, "note")
    log.info("zoho_note_added", module=module, zoho_id=record_id)
    first = ((result or {}).get("data") or [{}])[0]
    return ((first.get("details") or {}).get("id") if isinstance(first, dict) else None)
