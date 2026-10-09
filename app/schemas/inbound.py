"""Normalised inbound WhatsApp event.

Gallabox wraps Meta's Cloud API payload, and the exact envelope differs a
little between webhook versions and plans. Rather than bind to one rigid
schema, the parser looks for the message object in the shapes we know about
and normalises whatever it finds into `InboundEvent`.

This is deliberate: an unexpected envelope should degrade to "unrecognised
message" and be logged with the raw body, not 500 and make Gallabox retry
forever. `docs/integrations.md` explains how to confirm the real shape against
the client's account.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class InboundKind(StrEnum):
    TEXT = "text"
    REPLY = "reply"          # button tap or list selection
    LOCATION = "location"    # shared location pin
    CART = "cart"            # catalogue order / cart submission
    MEDIA = "media"          # voice note, photo, file... - nothing to read
    UNKNOWN = "unknown"


# Message types a customer sends that the bot cannot read. They still deserve
# an answer ("please type instead") rather than silence. Anything else we do
# not recognise - a reaction, a status receipt - stays UNKNOWN and is ignored:
# replying to a thumbs-up is noise, and replying to an event we cannot place
# risks the bot answering its own echoes.
MEDIA_TYPES = frozenset({
    "audio", "voice", "image", "video", "document", "sticker",
    "contacts", "unsupported",
})


@dataclass
class CartLine:
    retailer_id: str
    quantity: int
    item_price: str | None = None
    currency: str | None = None


@dataclass
class InboundEvent:
    whatsapp_number: str
    kind: InboundKind = InboundKind.UNKNOWN
    message_id: str = ""
    contact_name: str = ""
    text: str = ""
    reply_id: str = ""
    reply_title: str = ""
    latitude: float | None = None
    longitude: float | None = None
    cart_lines: list[CartLine] = field(default_factory=list)
    # Click-to-WhatsApp ad attribution (spec step 1).
    ad_id: str = ""
    campaign_id: str = ""
    referral: dict = field(default_factory=dict)
    # The Gallabox channel (business number) it arrived on. The webhook gets
    # every channel in the account; see integrations/gallabox/channel.py.
    channel_id: str = ""
    channel_number: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def is_actionable(self) -> bool:
        return self.kind is not InboundKind.UNKNOWN and bool(self.whatsapp_number)

    @property
    def choice(self) -> str:
        """What the customer 'said', whichever way they said it.

        A list selection id, a button id, or the typed text - handlers mostly
        care about the value, not the widget that produced it.
        """
        return (self.reply_id or self.text or "").strip()


def parse_inbound(body: dict) -> InboundEvent:
    """Normalise a Gallabox webhook body into an `InboundEvent`."""
    payload = _first_dict(body, "payload", "data") or body
    whatsapp = _first_dict(payload, "whatsapp", "message") or payload

    contact = _first_dict(payload, "contact", "from", "sender") or {}
    # Gallabox puts the sender on the message object as `whatsapp.from`, the
    # mirror of the `whatsapp.to` it records on outbound. Missing that meant a
    # perfectly good message parsed to a blank number and was dropped as
    # unrecognised, with the webhook still answering 200 - so Gallabox saw a
    # healthy endpoint while the customer got no reply.
    number = _clean_number(
        contact.get("phone")
        or contact.get("phoneNumber")
        or whatsapp.get("from")
        or whatsapp.get("phone")
        or payload.get("from")
        or payload.get("phone")
        or body.get("from")
        or ""
    )

    event = InboundEvent(
        whatsapp_number=number,
        message_id=str(
            payload.get("id") or payload.get("messageId")
            or whatsapp.get("id") or body.get("id") or ""
        ),
        contact_name=str(contact.get("name") or payload.get("name") or ""),
        channel_id=str(body.get("channelId") or payload.get("channelId") or ""),
        channel_number=str(body.get("channelNumber") or payload.get("channelNumber") or ""),
        raw=body,
    )

    _apply_referral(event, payload, whatsapp)

    msg_type = str(whatsapp.get("type") or payload.get("type") or "").lower()

    if msg_type == "text" or "text" in whatsapp:
        event.kind = InboundKind.TEXT
        event.text = str(_first_dict(whatsapp, "text").get("body")
                         or whatsapp.get("text") or "").strip()

    elif msg_type == "interactive" or "interactive" in whatsapp:
        _apply_interactive(event, _first_dict(whatsapp, "interactive"))

    elif msg_type == "button" or "button" in whatsapp:
        # Template quick-reply buttons arrive as type "button".
        button = _first_dict(whatsapp, "button")
        event.kind = InboundKind.REPLY
        event.reply_id = str(button.get("payload") or button.get("text") or "")
        event.reply_title = str(button.get("text") or "")

    elif msg_type == "location" or "location" in whatsapp:
        location = _first_dict(whatsapp, "location")
        event.kind = InboundKind.LOCATION
        event.latitude = _as_float(location.get("latitude") or location.get("lat"))
        event.longitude = _as_float(location.get("longitude") or location.get("lng")
                                    or location.get("long"))

    elif msg_type == "order" or "order" in whatsapp:
        _apply_cart(event, _first_dict(whatsapp, "order"))

    if event.kind is InboundKind.UNKNOWN and event.text:
        event.kind = InboundKind.TEXT

    if event.kind is InboundKind.UNKNOWN and (
        msg_type in MEDIA_TYPES or any(key in whatsapp for key in MEDIA_TYPES)
    ):
        event.kind = InboundKind.MEDIA

    return event


def _apply_interactive(event: InboundEvent, interactive: dict) -> None:
    """Button tap or list selection."""
    reply = (
        _first_dict(interactive, "button_reply", "buttonReply")
        or _first_dict(interactive, "list_reply", "listReply")
    )
    if reply:
        event.kind = InboundKind.REPLY
        event.reply_id = str(reply.get("id") or "")
        event.reply_title = str(reply.get("title") or "")
        return

    # A location request answered with a pin comes back as interactive too.
    location = _first_dict(interactive, "location")
    if location:
        event.kind = InboundKind.LOCATION
        event.latitude = _as_float(location.get("latitude"))
        event.longitude = _as_float(location.get("longitude"))


def _apply_cart(event: InboundEvent, order: dict) -> None:
    """Cart submitted from the catalogue (spec step 8)."""
    event.kind = InboundKind.CART
    for raw_item in order.get("product_items") or order.get("productItems") or []:
        retailer_id = (raw_item.get("product_retailer_id")
                       or raw_item.get("productRetailerId"))
        if not retailer_id:
            continue
        event.cart_lines.append(CartLine(
            retailer_id=str(retailer_id),
            quantity=int(raw_item.get("quantity") or 1),
            item_price=_as_str(raw_item.get("item_price")
                               or raw_item.get("itemPrice")),
            currency=_as_str(raw_item.get("currency")),
        ))
    if order.get("text"):
        event.text = str(order["text"])


def _apply_referral(event: InboundEvent, payload: dict, whatsapp: dict) -> None:
    """Pull Click-to-WhatsApp ad attribution out of the referral block."""
    referral = (
        _first_dict(whatsapp, "referral")
        or _first_dict(payload, "referral")
        or _first_dict(payload, "adReferral", "ad_referral")
    )
    if not referral:
        return
    event.referral = referral
    event.ad_id = str(referral.get("source_id") or referral.get("sourceId")
                      or referral.get("ad_id") or "")
    event.campaign_id = str(referral.get("campaign_id")
                            or referral.get("campaignId") or "")


# --- small helpers -----------------------------------------------------------
def _first_dict(container: Any, *keys: str) -> dict:
    """Return the first key whose value is a dict, else {}."""
    if not isinstance(container, dict):
        return {}
    for key in keys:
        value = container.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _clean_number(raw: Any) -> str:
    return "".join(ch for ch in str(raw or "") if ch.isdigit())
