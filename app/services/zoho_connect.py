"""One-click Zoho connection: the OAuth authorization-code flow.

Pasting a refresh token by hand means generating it in Zoho's API console,
copying a `grant_token` within its 10-minute life, and exchanging it with
curl. This does that exchange for the admin instead: press Connect, approve
on Zoho, and the refresh token is stored encrypted without ever being shown.

The data centre matters more than it looks: Zoho's data centres are isolated,
so a `.in` org rejects calls made to `.com`. Nobody has to pick it, though.
The consent screen starts at accounts.zoho.com, which signs in an account
from any data centre (with Multi-DC enabled on the client), and Zoho's reply
says where the account lives in its `location` parameter.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlencode

import httpx

from app.core.config import ZOHO_HOSTS, is_unset, settings
from app.core.exceptions import IntegrationError
from app.core.logging import get_logger

log = get_logger(__name__)

# What the bot actually does: read/write leads and orders, read users for
# owner assignment, module metadata to create and check custom fields, and
# which org the token belongs to (record ids are only valid in that org).
SCOPES = "ZohoCRM.modules.ALL,ZohoCRM.settings.ALL,ZohoCRM.users.READ,ZohoCRM.org.READ"

# The domain suffix picked next to Connect Zoho: where the consent screen
# opens. Zoho's reply still has the final say (see centre_from_location).
DATA_CENTRES = [
    ("com", "zoho.com"),
    ("in", "zoho.in"),
    ("eu", "zoho.eu"),
    ("au", "zoho.com.au"),
    ("jp", "zoho.jp"),
    ("ca", "zohocloud.ca"),
]

# Zoho's `location` values -> our ZOHO_DATA_CENTER keys (see ZOHO_HOSTS).
LOCATIONS = {"us": "com", "com": "com", "in": "in", "eu": "eu",
             "au": "au", "jp": "jp", "ca": "ca"}


def centre_from_location(location: str | None) -> str | None:
    """The data centre Zoho's callback reports, or None if it is unknown.

    Only mapped values count: the token exchange posts the client secret to
    that data centre's accounts host, so it must never come from the URL.
    """
    return LOCATIONS.get((location or "").strip().lower())


def accounts_host(data_centre: str) -> str:
    """Accounts host for one data centre, falling back to .com."""
    return ZOHO_HOSTS.get(data_centre, ZOHO_HOSTS["com"])[0]


def redirect_uri() -> str:
    """The callback Zoho sends the grant token back to.

    Must match the Authorized Redirect URI registered on the Zoho client
    character for character, including the scheme and any trailing path.
    """
    return f"{settings.public_base_url.rstrip('/')}/admin/settings/zoho/callback"


def authorize_url(client_id: str, data_centre: str, state: str) -> str:
    """Where to send the admin's browser to approve access."""
    query = urlencode({
        "scope": SCOPES,
        "client_id": client_id,
        "response_type": "code",
        # `offline` is what makes Zoho return a refresh token rather than
        # only a one-hour access token.
        "access_type": "offline",
        # Without this, a second connect attempt returns no refresh token
        # because the user already consented once.
        "prompt": "consent",
        "redirect_uri": redirect_uri(),
        "state": state,
    })
    return f"{accounts_host(data_centre)}/oauth/v2/auth?{query}"


def new_state() -> str:
    """Opaque value echoed back by Zoho, to detect a forged callback."""
    return secrets.token_urlsafe(24)


async def exchange_code(code: str, client_id: str, client_secret: str,
                        data_centre: str) -> str:
    """Trade the grant token for a refresh token.

    Zoho reports OAuth failures inside a 200 response, so the body is checked
    rather than the status code.
    """
    url = f"{accounts_host(data_centre)}/oauth/v2/token"
    payload = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri(),
        "code": code,
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(url, data=payload)
    except httpx.HTTPError as exc:
        raise IntegrationError("zoho", f"Could not reach Zoho: {exc}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise IntegrationError(
            "zoho",
            f"Zoho returned a non-JSON response (HTTP {response.status_code}).",
            status_code=response.status_code,
        ) from exc

    if "error" in data:
        raise IntegrationError("zoho", _explain(str(data["error"])))

    token = data.get("refresh_token")
    if not token:
        # Happens when the user has already granted consent and Zoho reissues
        # only an access token.
        raise IntegrationError(
            "zoho",
            "Zoho did not return a refresh token. Remove this app under "
            "Zoho Accounts -> Connected Apps and connect again."
        )
    return str(token)


def _explain(code: str) -> str:
    """Turn Zoho's terse OAuth error codes into something actionable."""
    return {
        "invalid_code": "That authorisation expired - grant tokens last about "
                        "10 minutes. Press Connect again.",
        "invalid_client": "The Client ID or Client Secret is wrong, or belongs "
                          "to a different data centre.",
        "invalid_client_secret": "The Client Secret is wrong.",
        "redirect_uri_mismatch": f"Zoho rejected the redirect URI. Register "
                                 f"exactly this one on the client: "
                                 f"{redirect_uri()}",
        "access_denied": "Access was declined on the Zoho consent screen.",
    }.get(code, f"Zoho rejected the connection ({code}).")


# --- which org -----------------------------------------------------------------
async def record_org(session, *, updated_by: str | None = None) -> tuple[str | None, int]:
    """Remember which Zoho org the bot is connected to; forget links to any other.

    Zoho record ids belong to one org. After connecting a different org, the
    ids the bot saved point at records that do not exist there, so they are
    dropped and Push to Zoho (or the customer's next message) recreates the
    records. Returns (org id, number of customers whose links were dropped).
    """
    # Imported here: this module is also used by the settings routes before
    # the CRM layer is wanted, and the CRM layer reaches back into settings.
    from app.core.exceptions import ConfigurationError
    from app.integrations.zoho import crm
    from app.services import customer_admin
    from app.services.settings_store import save_many

    try:
        org_id = await crm.org_id()
    except (IntegrationError, ConfigurationError) as exc:
        log.warning("zoho_org_lookup_failed", error=str(exc))
        return None, 0

    previous = settings.zoho_org_id
    dropped = 0
    if org_id and previous and org_id != previous:
        dropped = await customer_admin.forget_zoho_links(session)
        log.info("zoho_org_changed", previous=previous, current=org_id,
                 links_dropped=dropped)
    if org_id and org_id != previous:
        await save_many(session, {"ZOHO_ORG_ID": org_id}, updated_by=updated_by)
    return org_id, dropped


def is_connected() -> bool:
    """Credentials and a refresh token are in place (not whether they work)."""
    return not (is_unset(settings.zoho_client_id) or is_unset(settings.zoho_client_secret)
                or is_unset(settings.zoho_refresh_token))
