"""Signed links to the web ordering page.

A customer taps one link in WhatsApp and lands on the storefront already
identified - no login, no code to type. The customer id travels inside a
signed, expiring token rather than as a plain query parameter, so the URL
cannot be edited to order as somebody else.

The token is deliberately short-lived. A link forwarded to a friend, or left
in a chat for a week, stops working rather than giving a stranger access to
the customer's saved address.
"""

from __future__ import annotations

import uuid

from urllib.parse import quote

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Customer
from app.services.conversation.prompts import NEW_LINK_REQUEST

log = get_logger(__name__)

SALT = "shero-order-link"
LINK_EXPIRED = "This ordering link has expired."
# Long enough to browse a 287-dish menu without rushing, short enough that a
# forwarded link is not a standing invitation.
MAX_AGE_SECONDS = 6 * 60 * 60


def _serializer() -> URLSafeTimedSerializer:
    """Signer keyed on the admin session secret.

    Reuses that secret rather than adding another required setting; rotating
    it invalidates open ordering links, which is the safe direction.
    """
    secret = settings.admin_session_secret or settings.settings_encryption_key
    if not secret:
        raise RuntimeError(
            "ADMIN_SESSION_SECRET is required to sign ordering links."
        )
    return URLSafeTimedSerializer(secret, salt=SALT)


def build_token(customer_id: uuid.UUID | str) -> str:
    return _serializer().dumps(str(customer_id))


# Pages a link may open instead of the menu. "address" is the address-only
# page behind the order summary's Update location button. Anything else
# gets the menu.
OPEN_AT = {"address": "location"}


def build_url(customer_id: uuid.UUID | str, *, open_at: str | None = None) -> str:
    """The full link to put in a WhatsApp message."""
    base = settings.public_base_url.rstrip("/")
    return f"{base}/order/{link_suffix(customer_id, open_at=open_at)}"


def link_suffix(customer_id: uuid.UUID | str, *, open_at: str | None = None) -> str:
    """Everything after /order/ - the menu_link template's button parameter."""
    token = build_token(customer_id)
    page = OPEN_AT.get(open_at or "")
    return f"{token}/{page}" if page else token


def read_token(token: str) -> uuid.UUID | None:
    """Customer id from a token, or None if it is expired or forged."""
    try:
        raw = _serializer().loads(token, max_age=MAX_AGE_SECONDS)
    except SignatureExpired:
        log.info("order_link_expired")
        return None
    except BadSignature:
        log.warning("order_link_bad_signature")
        return None

    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError):
        log.warning("order_link_bad_payload")
        return None


async def customer_for_token(session: AsyncSession, token: str) -> Customer | None:
    """The customer a page request is acting for, or None for a dead link."""
    customer_id = read_token(token)
    if customer_id is None:
        return None
    return await session.get(Customer, customer_id)


def dead_link_response() -> dict:
    """What every JSON route returns for an old or forged link.

    `expired` lets the page swap to its "get a new link" screen instead of
    showing one error message per button the customer taps.
    """
    return {"ok": False, "expired": True, "error": LINK_EXPIRED,
            "new_link_url": new_link_whatsapp_url()}


def new_link_whatsapp_url() -> str:
    """Opens the business chat with "New menu link" already typed.

    One tap on send and the bot replies with a fresh link (the words are in
    NEW_LINK_KEYWORDS). Empty when no business number is configured.
    """
    chat = whatsapp_chat_url()
    return f"{chat}?text={quote(NEW_LINK_REQUEST)}" if chat else ""


def whatsapp_chat_url() -> str:
    """A link that opens the business chat in WhatsApp, or "" if unset.

    wa.me opens the app on a phone and WhatsApp Web on a desktop, which is
    where the payment link has just been sent.
    """
    digits = "".join(ch for ch in settings.whatsapp_business_number if ch.isdigit())
    return f"https://wa.me/{digits}" if digits else ""
