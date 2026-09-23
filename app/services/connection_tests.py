"""One read-only probe per integration, for the Settings page Test buttons.

Each returns a plain-language verdict rather than a stack trace, because the
person pressing the button is the client, not an engineer.

Every probe is read-only and safe to press repeatedly. Nothing here sends a
WhatsApp message, charges a card or writes a CRM record - the Gallabox check
deliberately stops at "are these credentials accepted", since the only way to
prove a send works is to send, and that is a deliberate action elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.core.config import is_unset, settings
from app.core.exceptions import IntegrationError
from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass
class TestResult:
    service: str
    ok: bool
    message: str


async def test_zoho() -> TestResult:
    """Exchange the refresh token, then read the org record."""
    missing = _missing({
        "Client ID": settings.zoho_client_id,
        "Client Secret": settings.zoho_client_secret,
        "Refresh Token": settings.zoho_refresh_token,
    })
    if missing:
        return TestResult("Zoho CRM", False, f"Not configured - missing {missing}.")

    from app.integrations.zoho.oauth import get_access_token
    from app.integrations.zoho.client import zoho_client

    try:
        await get_access_token(force_refresh=True)
    except Exception as exc:  # noqa: BLE001 - message is for a non-engineer
        return TestResult("Zoho CRM", False, _zoho_hint(str(exc)))

    try:
        payload = await zoho_client.get("/crm/v6/org")
    except IntegrationError as exc:
        return TestResult("Zoho CRM", False,
                          f"Signed in, but the API rejected the call: {exc.message}")

    orgs = (payload or {}).get("org") or []
    name = orgs[0].get("company_name", "your org") if orgs else "your org"
    return TestResult("Zoho CRM", True,
                      f"Connected to {name} ({settings.zoho_data_center}).")


def _zoho_hint(error: str) -> str:
    lowered = error.lower()
    if "invalid_client" in lowered:
        return ("Client ID or Client Secret is wrong, or was issued in a "
                "different Zoho data centre.")
    if "invalid_code" in lowered or "invalid_grant" in lowered:
        return "The refresh token is invalid or has been revoked. Generate a new one."
    if "invalid" in lowered and "scope" in lowered:
        return ("The token is missing a scope. It needs ZohoCRM.modules.ALL, "
                "ZohoCRM.settings.ALL and ZohoCRM.users.READ.")
    return f"Could not sign in: {error[:200]}"


async def test_gallabox() -> TestResult:
    """Check the credentials are accepted, without sending a message."""
    missing = _missing({
        "API Key": settings.gallabox_api_key,
        "API Secret": settings.gallabox_api_secret,
        "Channel ID": settings.gallabox_channel_id,
    })
    if missing:
        return TestResult("WhatsApp (Gallabox)", False,
                          f"Not configured - missing {missing}.")

    if settings.gallabox_channel_id.strip() == settings.gallabox_api_key.strip():
        return TestResult(
            "WhatsApp (Gallabox)", False,
            "Channel ID is the same as the API Key. Both are 24 characters, so "
            "the API key was probably pasted into the channel field.",
        )

    headers = {
        "apiKey": settings.gallabox_api_key,
        "apiSecret": settings.gallabox_api_secret,
    }
    url = f"{settings.gallabox_base_url.rstrip('/')}/contacts"

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(url, headers=headers, params={"limit": 1})
    except httpx.HTTPError as exc:
        return TestResult("WhatsApp (Gallabox)", False, f"Could not reach Gallabox: {exc}")

    if response.status_code in (200, 201, 204):
        return TestResult("WhatsApp (Gallabox)", True,
                          "Credentials accepted. Send a test message to confirm "
                          "the key can also send.")
    if response.status_code in (401, 403):
        return TestResult("WhatsApp (Gallabox)", False,
                          "Rejected - check the API Key and Secret, and that the "
                          "key's permission scope is not Read only.")
    return TestResult("WhatsApp (Gallabox)", False,
                      f"Unexpected response (HTTP {response.status_code}). "
                      f"{response.text[:160]}")


async def test_stripe() -> TestResult:
    if is_unset(settings.stripe_secret_key):
        return TestResult("Stripe", False, "Not configured - missing the Secret Key.")

    import stripe

    stripe.api_key = settings.stripe_secret_key
    try:
        account = await stripe.Account.retrieve_async()
    except stripe.AuthenticationError:
        return TestResult("Stripe", False, "The secret key was rejected.")
    except stripe.StripeError as exc:
        return TestResult("Stripe", False, f"Stripe returned an error: {exc}")

    mode = "TEST mode" if settings.stripe_secret_key.startswith("sk_test") else "LIVE mode"
    name = account.get("business_profile", {}).get("name") or account.get("id")
    charges_on = account.get("charges_enabled")
    detail = "charges enabled" if charges_on else "charges NOT yet enabled"
    return TestResult("Stripe", bool(charges_on),
                      f"Connected to {name} in {mode} - {detail}.")


async def test_uber() -> TestResult:
    missing = _missing({
        "Customer ID": settings.uber_customer_id,
        "Client ID": settings.uber_client_id,
        "Client Secret": settings.uber_client_secret,
    })
    if missing:
        return TestResult("Uber Direct", False, f"Not configured - missing {missing}.")

    from app.integrations.uber.oauth import get_access_token

    try:
        await get_access_token(force_refresh=True)
    except Exception as exc:  # noqa: BLE001
        return TestResult("Uber Direct", False,
                          f"Could not get a token: {str(exc)[:200]}")
    return TestResult("Uber Direct", True,
                      "Credentials accepted. A delivery quote also needs the "
                      "kitchen address to be set below.")


async def test_meta() -> TestResult:
    missing = _missing({
        "Catalogue ID": settings.meta_catalog_id,
        "System User Token": settings.meta_system_user_token,
    })
    if missing:
        return TestResult("Meta Catalogue", False, f"Not configured - missing {missing}.")

    from app.integrations.meta.catalog import meta_catalog

    try:
        products = await meta_catalog.list_products(force_refresh=True)
    except IntegrationError as exc:
        return TestResult("Meta Catalogue", False, exc.message[:220])

    return TestResult("Meta Catalogue", True,
                      f"Connected - {len(products)} product(s) in the catalogue.")


async def test_maps() -> TestResult:
    if is_unset(settings.google_maps_api_key):
        return TestResult(
            "Maps", True,
            "No key set. ZIP lookups use OpenStreetMap, which is free but "
            "rate-limited. Location pins work either way.",
        )

    from app.integrations.geo.geocoder import clear_cache, geocode_postal_code

    clear_cache()
    point = await geocode_postal_code("21075", "US")
    if point is None:
        return TestResult("Maps", False,
                          "The key did not resolve a known ZIP. Check it has the "
                          "Geocoding API enabled and no referrer restriction.")
    return TestResult("Maps", True,
                      f"Geocoding works ({point.latitude:.3f}, {point.longitude:.3f}).")


def _missing(fields: dict[str, str]) -> str:
    names = [name for name, value in fields.items() if is_unset(value)]
    return ", ".join(names)


TESTS = {
    "zoho": test_zoho,
    "gallabox": test_gallabox,
    "stripe": test_stripe,
    "uber": test_uber,
    "meta": test_meta,
    "maps": test_maps,
}
