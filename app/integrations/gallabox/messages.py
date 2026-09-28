"""WhatsApp message payload builders.

Pure functions - no I/O - so the exact bytes we send can be unit tested.

The `whatsapp` block follows Meta's Cloud API interactive-object shape, which
Gallabox passes through. WhatsApp silently rejects over-long labels, so the
limits below are enforced by truncation rather than left to chance:

    reply button title   20 chars    (max 3 buttons)
    list row title       24 chars    (max 10 rows across all sections)
    list row description 72 chars
    list action button   20 chars
"""

from __future__ import annotations

from dataclasses import dataclass

BUTTON_TITLE_LIMIT = 20
ROW_TITLE_LIMIT = 24
ROW_DESCRIPTION_LIMIT = 72
LIST_BUTTON_LIMIT = 20
MAX_REPLY_BUTTONS = 3
MAX_LIST_ROWS = 10


def clip(text: str, limit: int) -> str:
    """Truncate to `limit`, ending with an ellipsis when shortened."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"


@dataclass(frozen=True)
class Button:
    id: str
    title: str


@dataclass(frozen=True)
class ListRow:
    id: str
    title: str
    description: str = ""


@dataclass(frozen=True)
class ListSection:
    title: str
    rows: list[ListRow]


def text_message(body: str) -> dict:
    return {"type": "text", "text": {"body": body}}


def button_message(body: str, buttons: list[Button], *, header: str | None = None,
                   footer: str | None = None) -> dict:
    """Reply-button message (spec steps 5, 14, 19)."""
    if not 1 <= len(buttons) <= MAX_REPLY_BUTTONS:
        raise ValueError(f"WhatsApp allows 1-{MAX_REPLY_BUTTONS} reply buttons, got {len(buttons)}")

    interactive: dict = {
        "type": "button",
        "body": {"text": body},
        "action": {
            "buttons": [
                {"type": "reply",
                 "reply": {"id": b.id, "title": clip(b.title, BUTTON_TITLE_LIMIT)}}
                for b in buttons
            ]
        },
    }
    if header:
        interactive["header"] = {"type": "text", "text": header}
    if footer:
        interactive["footer"] = {"text": footer}
    return {"type": "interactive", "interactive": interactive}


def list_message(body: str, sections: list[ListSection], *, button_text: str = "Choose",
                 header: str | None = None, footer: str | None = None) -> dict:
    """Single-select list message (spec steps 6, 11, 13)."""
    total_rows = sum(len(s.rows) for s in sections)
    if not 1 <= total_rows <= MAX_LIST_ROWS:
        raise ValueError(f"WhatsApp allows 1-{MAX_LIST_ROWS} list rows, got {total_rows}")

    interactive: dict = {
        "type": "list",
        "body": {"text": body},
        "action": {
            "button": clip(button_text, LIST_BUTTON_LIMIT),
            "sections": [
                {
                    "title": clip(section.title, ROW_TITLE_LIMIT),
                    "rows": [
                        {
                            "id": row.id,
                            "title": clip(row.title, ROW_TITLE_LIMIT),
                            **({"description": clip(row.description, ROW_DESCRIPTION_LIMIT)}
                               if row.description else {}),
                        }
                        for row in section.rows
                    ],
                }
                for section in sections
            ],
        },
    }
    if header:
        interactive["header"] = {"type": "text", "text": header}
    if footer:
        interactive["footer"] = {"text": footer}
    return {"type": "interactive", "interactive": interactive}


def cta_url_message(body: str, *, url: str, display_text: str,
                    header: str | None = None, footer: str | None = None) -> dict:
    """A single button that opens a link (Meta's `cta_url` interactive).

    Used for the ordering link. A bare URL in a text message is easy to miss
    and easy to mistype; this renders as a button and opens in WhatsApp's own
    browser, so the customer never leaves the app.

    Unlike a template's URL button this needs no approval and carries the
    whole URL, which matters here: the ordering link ends in a signed token
    far too long to pass as a template button parameter. It is only valid
    inside the 24-hour customer service window, which is exactly where the
    menu link is sent.
    """
    if not url:
        raise ValueError("a cta_url message needs a url")

    interactive: dict = {
        "type": "cta_url",
        "body": {"text": body},
        "action": {
            "name": "cta_url",
            "parameters": {
                "display_text": clip(display_text, BUTTON_TITLE_LIMIT),
                "url": url,
            },
        },
    }
    if header:
        interactive["header"] = {"type": "text", "text": header}
    if footer:
        interactive["footer"] = {"text": footer}
    return {"type": "interactive", "interactive": interactive}


def location_request_message(body: str) -> dict:
    """Native "share your location" prompt (spec steps 6a and 9).

    The customer may still reply with a typed ZIP instead; the handler accepts
    either, because this button is unavailable on some WhatsApp clients.
    """
    return {
        "type": "interactive",
        "interactive": {
            "type": "location_request_message",
            "body": {"text": body},
            "action": {"name": "send_location"},
        },
    }


def product_list_message(catalog_id: str, sections: list[dict], *, header: str,
                         body: str, footer: str | None = None) -> dict:
    """Multi-product message from the Meta catalogue (spec step 7).

    `sections` is [{"title": str, "product_items": [{"product_retailer_id": str}]}].
    """
    interactive: dict = {
        "type": "product_list",
        "header": {"type": "text", "text": clip(header, 60)},
        "body": {"text": body},
        "action": {"catalog_id": catalog_id, "sections": sections},
    }
    if footer:
        interactive["footer"] = {"text": footer}
    return {"type": "interactive", "interactive": interactive}


def url_button_value(index: int, text: str) -> dict:
    """The runtime part of a dynamic URL button, in Gallabox's shape.

    Gallabox wants `buttonValues` as a list of these - a dict keyed by index
    is rejected with a 500 ("buttonValues.find is not a function").
    """
    return {"index": index, "sub_type": "url",
            "parameters": {"type": "text", "text": text}}


def template_message(name: str, body_values: list[str], *,
                     button_values: list[dict] | None = None,
                     language: str = "en") -> dict:
    """Pre-approved template (spec section 3).

    Required outside the 24-hour customer service window - which is exactly
    when every payment and delivery notification fires.
    """
    template: dict = {
        "templateName": name,
        "language": language,
        # Gallabox keys body params by 1-based position, matching {{1}}, {{2}}.
        "bodyValues": {str(i): str(v) for i, v in enumerate(body_values, start=1)},
    }
    if button_values:
        template["buttonValues"] = button_values
    return {"type": "template", "template": template}
