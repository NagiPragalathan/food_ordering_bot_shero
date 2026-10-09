"""Is a template usable? Asked before sending one that has a fallback.

Gallabox accepts a send for a template Meta has not approved yet (202), and
the message then fails quietly on Meta's side. A send-time error therefore
cannot be relied on to trigger a fallback - the customer would simply get
nothing. So for templates that have a fallback (menu_link, order_summary),
the sender checks first.

"Usable" is two things: approved, and approved at least 15 minutes ago -
Gallabox refuses a template for its first 15 minutes ("to avoid message
failure"), although its API already says "approved" then. The 15 minutes
are measured on Gallabox's own clock (the Date header of its API), because
this machine's clock cannot be trusted to agree with Gallabox's timestamps.

A template whose link button points at another website is not usable
either: the button's address is fixed when Meta approves it, so after the
bot moves to a new address (PUBLIC_BASE_URL) the old template would send
customers to a dead link. The fallback carries the current link instead.

The answer is cached for a few minutes: this sits on the path of every
menu link. If Gallabox cannot be asked, the answer is "not usable" - the
fallback always works, the template might not.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.integrations.gallabox import template_admin

log = get_logger(__name__)

CACHE_SECONDS = 300
SETTLE = timedelta(minutes=15)

_usable: dict[str, bool] = {}
_fetched_at: float = 0.0


async def is_approved(name: str) -> bool:
    """Approved, and approved long enough ago for Gallabox to send it."""
    global _fetched_at
    if time.monotonic() - _fetched_at > CACHE_SECONDS:
        try:
            rows = await template_admin.list_templates()
        except Exception as exc:   # any failure: treat as unusable, use the fallback
            log.warning("template_status_unavailable", error=str(exc))
            return False
        now = await gallabox_now()
        _usable.clear()
        _usable.update({str(row.get("name")): usable(row, now) for row in rows})
        _fetched_at = time.monotonic()
    return _usable.get(name, False)


def usable(row: dict, now: datetime | None) -> bool:
    if str(row.get("status") or "").lower() != "approved":
        return False
    if _recategorised_as_marketing(row):
        log.warning("template_recategorised_marketing", template=row.get("name"))
        return False
    stale = _foreign_button_url(row)
    if stale:
        log.warning("template_link_outdated", template=row.get("name"), button_url=stale,
                    public_base_url=settings.public_base_url)
        return False
    approved_at = _parse_iso(row.get("statusUpdatedAt"))
    if now is None or approved_at is None:
        # Cannot tell how recent it is. Only a fresh approval is at risk, and
        # the fallback still delivers, so err towards the fallback.
        return False
    return now - approved_at >= SETTLE


async def gallabox_now() -> datetime | None:
    """Gallabox's current time, from its Date header. None if unreachable."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.head(settings.gallabox_base_url)
        return parsedate_to_datetime(response.headers["date"])
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        log.warning("gallabox_clock_unavailable", error=str(exc))
        return None


def _foreign_button_url(row: dict) -> str | None:
    """A URL button pointing at a different website than this bot's, if any."""
    ours = {_origin(settings.public_base_url), _origin(settings.pay_redirect_base_url)}
    for component in row.get("components") or []:
        for button in (component.get("buttons") or []) if isinstance(component, dict) else []:
            url = str(button.get("url") or "")
            # Only per-customer links ({{1}}) point at this bot.
            if "{{" not in url:
                continue
            if str(button.get("type") or "").upper() == "URL" and _origin(url) not in ours:
                return url
    return None


def _recategorised_as_marketing(row: dict) -> bool:
    """A template we submitted as UTILITY that Meta now files as MARKETING."""
    # Imported here: templates is a plain catalogue, kept free of this module.
    from app.integrations.gallabox.templates import BY_NAME

    spec = BY_NAME.get(str(row.get("name")))
    category = str(row.get("category") or "").upper()
    return bool(spec and spec.category == "UTILITY" and category == "MARKETING")


def _origin(url: str) -> str:
    parts = urlsplit(url.strip())
    return f"{parts.scheme}://{parts.netloc}".lower()


def _parse_iso(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def clear_cache() -> None:
    global _fetched_at
    _usable.clear()
    _fetched_at = 0.0
