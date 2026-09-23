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

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

SALT = "shero-order-link"
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


def build_url(customer_id: uuid.UUID | str) -> str:
    """The full link to put in a WhatsApp message."""
    base = settings.public_base_url.rstrip("/")
    return f"{base}/order/{build_token(customer_id)}"


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
