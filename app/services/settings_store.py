"""Runtime settings: environment floor, database overrides.

The admin Settings page writes here, so the client can rotate a Zoho secret or
repoint the WhatsApp channel without a redeploy.

Resolution order is database -> environment. On save, the values are applied
straight onto the live `settings` singleton, so every integration client picks
them up without a restart, and any cached OAuth token for a changed service is
dropped.

Secrets are encrypted at rest with Fernet. The key comes from
SETTINGS_ENCRYPTION_KEY; without it, secrets are refused rather than written
in plaintext - a database dump should never hand over the Stripe key.

Multi-instance note: an override saved on one instance reaches the others when
the `refresh_settings` job runs (every minute), not instantly.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import is_unset, settings
from app.core.logging import get_logger
from app.db.models import AppSetting

log = get_logger(__name__)

# Keys the admin Settings page may write, grouped for the UI. `secret=True`
# values are encrypted at rest and never rendered back to the browser.
SETTING_GROUPS: dict[str, list[tuple[str, str, bool]]] = {
    "WhatsApp (Gallabox)": [
        ("GALLABOX_API_KEY", "API Key", False),
        ("GALLABOX_API_SECRET", "API Secret", True),
        ("GALLABOX_CHANNEL_ID", "Channel ID", False),
        ("GALLABOX_WEBHOOK_TOKEN", "Webhook Token", True),
    ],
    "Zoho CRM": [
        ("ZOHO_CLIENT_ID", "Client ID", False),
        ("ZOHO_CLIENT_SECRET", "Client Secret", True),
        ("ZOHO_REFRESH_TOKEN", "Refresh Token", True),
        ("ZOHO_DATA_CENTER", "Data Centre (com / in / eu / au / jp / ca)", False),
        ("ZOHO_ORDERS_MODULE", "Orders Module API Name", False),
        ("ZOHO_ORDER_ITEMS_MODULE", "Order Items Module API Name", False),
        ("ZOHO_ORG_ID", "Org ID (recorded by Connect Zoho)", False),
    ],
    "Stripe": [
        ("STRIPE_SECRET_KEY", "Secret Key", True),
        ("STRIPE_PUBLISHABLE_KEY", "Publishable Key", False),
        ("STRIPE_WEBHOOK_SECRET", "Webhook Secret", True),
        ("STRIPE_CURRENCY", "Currency", False),
    ],
    "Uber Direct": [
        ("UBER_CUSTOMER_ID", "Customer ID", False),
        ("UBER_CLIENT_ID", "Client ID", False),
        ("UBER_CLIENT_SECRET", "Client Secret", True),
    ],
    "Meta Catalogue": [
        ("META_CATALOG_ID", "Catalogue ID", False),
        ("META_SYSTEM_USER_TOKEN", "System User Token", True),
    ],
    "Maps": [
        ("GOOGLE_MAPS_API_KEY", "Google Maps API Key (server: Geocoding API)", True),
        ("GOOGLE_MAPS_BROWSER_KEY",
         "Google Maps Browser Key (Maps JavaScript API, referrer-restricted)", False),
    ],
    "Business rules": [
        ("TAX_PERCENT", "Tax percent applied to the dish subtotal", False),
        ("PAYMENT_LINK_TTL_MINUTES", "Payment link expiry (minutes, min 30)", False),
        ("PAYMENT_REMINDER_MINUTES", "Unpaid reminder after (minutes)", False),
        ("FEEDBACK_DELAY_MINUTES", "Feedback request after delivery (minutes)", False),
        ("PUBLIC_BASE_URL", "Public API URL", False),
        ("PAY_REDIRECT_BASE_URL", "Pay redirect URL", False),
    ],
}

# Saved from their own admin pages (Bot replies), not the Settings form.
OTHER_EDITABLE: list[tuple[str, str, bool]] = [
    ("BOT_REPLY_MODE", "Who the bot replies to (all / allowlist)", False),
    ("BOT_ALLOWED_NUMBERS", "Whitelisted WhatsApp numbers", False),
]

EDITABLE_KEYS: dict[str, tuple[str, bool]] = {
    key: (label, secret)
    for fields in [*SETTING_GROUPS.values(), OTHER_EDITABLE]
    for key, label, secret in fields
}

# Changing any of these invalidates a cached OAuth token.
TOKEN_INVALIDATORS = {
    "ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN",
    "ZOHO_DATA_CENTER", "UBER_CLIENT_ID", "UBER_CLIENT_SECRET",
}


class SecretsUnavailable(RuntimeError):
    """SETTINGS_ENCRYPTION_KEY is missing, so secrets cannot be stored."""


def _fernet() -> Fernet:
    raw = settings.settings_encryption_key
    if is_unset(raw):
        raise SecretsUnavailable(
            "SETTINGS_ENCRYPTION_KEY is not set, so secret settings cannot be "
            "saved. Generate one with: python -c "
            '"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    # Accept either a proper Fernet key or any passphrase, derived to 32 bytes.
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError):
        digest = hashlib.sha256(raw.encode()).digest()
        return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str | None:
    """Return the plaintext, or None if it cannot be decrypted.

    A key rotation leaves old rows unreadable; returning None means the
    environment value is used instead of crashing the service.
    """
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, SecretsUnavailable, ValueError):
        return None


# --- reading -----------------------------------------------------------------
async def load_overrides(session: AsyncSession) -> dict[str, str]:
    """Every stored override, decrypted, keyed by setting name."""
    result = await session.execute(select(AppSetting))
    overrides: dict[str, str] = {}

    for row in result.scalars():
        if row.value is None:
            continue
        value = decrypt(row.value) if row.is_secret else row.value
        if value is None:
            log.warning("setting_undecryptable", key=row.key)
            continue
        overrides[row.key] = value
    return overrides


async def apply_overrides(session: AsyncSession) -> int:
    """Copy stored overrides onto the live settings singleton.

    Mutating the singleton is deliberate: every integration client already
    reads from it, so one write reaches all of them without threading a
    session through the whole call graph.
    """
    overrides = await load_overrides(session)
    applied = 0

    # An override that was applied earlier but is gone now (cleared by
    # another process, say a script's disconnect) falls back to the
    # environment, so the minute-ly refresh really does drop a token.
    for key in _applied - set(overrides):
        _restore_default(key)
    _applied.clear()

    for key, value in overrides.items():
        field = key.lower()
        if not hasattr(settings, field):
            log.warning("setting_unknown_key", key=key)
            continue
        try:
            coerced = _coerce(field, value)
        except (TypeError, ValueError):
            log.warning("setting_bad_value", key=key)
            continue
        object.__setattr__(settings, field, coerced)
        _applied.add(key)
        applied += 1

    if applied:
        log.info("settings_overrides_applied", count=applied)
    return applied


_applied: set[str] = set()      # override keys currently applied in this process


def _restore_default(key: str) -> None:
    """Put the environment's value back on the live settings, right away.

    Without this a cleared override lingers in memory until a restart, and
    "Disconnect Zoho" would keep using the forgotten refresh token.
    """
    field = key.lower()
    if not hasattr(settings, field):
        return
    fresh = type(settings)()            # re-reads .env and the environment
    object.__setattr__(settings, field, getattr(fresh, field))
    _applied.discard(key)


def _coerce(field: str, value: str):
    """Cast a stored string to the type the settings field declares."""
    current = getattr(settings, field)
    if isinstance(current, bool):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(current, int):
        return int(value)
    if isinstance(current, float):
        return float(value)
    return value


# --- writing -----------------------------------------------------------------
async def save(session: AsyncSession, key: str, value: str, *,
               updated_by: str | None = None) -> None:
    """Store one override. Blank clears it, falling back to the environment."""
    if key not in EDITABLE_KEYS:
        raise ValueError(f"{key} is not an editable setting")

    _, is_secret = EDITABLE_KEYS[key]
    value = (value or "").strip()

    result = await session.execute(select(AppSetting).where(AppSetting.key == key))
    row = result.scalar_one_or_none()

    if not value:
        if row is not None:
            await session.delete(row)
            log.info("setting_cleared", key=key, updated_by=updated_by)
        _restore_default(key)
        return

    stored = encrypt(value) if is_secret else value
    if row is None:
        session.add(AppSetting(key=key, value=stored, is_secret=is_secret,
                               updated_by=updated_by))
    else:
        row.value = stored
        row.is_secret = is_secret
        row.updated_by = updated_by

    log.info("setting_saved", key=key, updated_by=updated_by)


async def save_many(session: AsyncSession, values: dict[str, str], *,
                    updated_by: str | None = None,
                    allow_blank: set[str] | None = None) -> list[str]:
    """Save a form's worth of settings. Returns the keys that changed.

    `allow_blank` names keys whose blank value means "clear this", which is
    how disconnecting an integration erases its stored token. Everything else
    keeps the safe default described below.
    """
    clearable = allow_blank or set()
    changed: list[str] = []

    for key, value in values.items():
        if key not in EDITABLE_KEYS:
            continue
        # A blank secret field means "leave it alone", not "clear it" - the
        # form never renders an existing secret back to the browser, so an
        # untouched field arrives empty on every save.
        _, is_secret = EDITABLE_KEYS[key]
        if is_secret and not (value or "").strip() and key not in clearable:
            continue
        # "Clear it" must reach the database even when this process never
        # loaded the override (a script, or a server before startup finished):
        # the in-memory value is blank then, which is not the same thing.
        if key not in clearable and str(_current_value(key)) == (value or "").strip():
            continue
        await save(session, key, value, updated_by=updated_by)
        changed.append(key)

    await session.flush()
    await apply_overrides(session)
    _invalidate_tokens(changed)
    return changed


def _current_value(key: str):
    return getattr(settings, key.lower(), "")


def _invalidate_tokens(changed: list[str]) -> None:
    """Drop cached OAuth tokens for any service whose credentials changed."""
    if not TOKEN_INVALIDATORS.intersection(changed):
        return
    from app.integrations.uber.oauth import invalidate_token as drop_uber
    from app.integrations.zoho.oauth import invalidate_token as drop_zoho

    drop_zoho()
    drop_uber()
    log.info("oauth_tokens_invalidated", reason="credentials changed")


async def stored_keys(session: AsyncSession) -> set[str]:
    """Keys that have a database override, for showing 'set' in the UI."""
    result = await session.execute(select(AppSetting.key))
    return set(result.scalars())
