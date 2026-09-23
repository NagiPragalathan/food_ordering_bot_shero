"""Webhook authenticity checks.

Both inbound webhooks are public endpoints, so each one is verified before the
payload is trusted:

* Stripe   - HMAC signature via the official SDK (`STRIPE_WEBHOOK_SECRET`).
* Gallabox - shared bearer token we generate and configure on their side
             (`GALLABOX_WEBHOOK_TOKEN`), compared in constant time.
"""

from __future__ import annotations

import hmac

from app.core.config import settings


def verify_gallabox_token(provided: str | None) -> bool:
    """Constant-time compare of the Gallabox webhook shared secret."""
    expected = settings.gallabox_webhook_token
    if not expected:
        # Fail closed in production; allow local development without the token.
        return not settings.is_production
    if not provided:
        return False
    # Accept both "Bearer <token>" and a bare token.
    if provided.lower().startswith("bearer "):
        provided = provided[7:]
    return hmac.compare_digest(provided.strip(), expected)
