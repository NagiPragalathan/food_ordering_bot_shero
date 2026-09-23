"""Zoho OAuth 2.0 - refresh-token grant.

The client id/secret pair comes from a Server-based or Self Client app in the
Zoho API console for the client's data centre. The refresh token is long-lived;
access tokens last an hour, so they are cached (see `TokenCache`) rather than
re-minted per call - Zoho rate-limits token generation hard (about 15 per
10 minutes per refresh token).
"""

from __future__ import annotations

import httpx

from app.core.config import settings
from app.core.exceptions import ConfigurationError, IntegrationError
from app.core.logging import get_logger
from app.integrations.base import TokenCache

log = get_logger(__name__)

_token_cache = TokenCache(leeway_seconds=120)


async def get_access_token(*, force_refresh: bool = False) -> str:
    """Return a valid Zoho access token, refreshing only when needed."""
    if not force_refresh:
        cached = _token_cache.get()
        if cached:
            return cached

    async with _token_cache.lock:
        # Another coroutine may have refreshed while we waited for the lock.
        if not force_refresh:
            cached = _token_cache.get()
            if cached:
                return cached

        if not (settings.zoho_client_id and settings.zoho_client_secret
                and settings.zoho_refresh_token):
            raise ConfigurationError(
                "Zoho credentials missing: set ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET "
                "and ZOHO_REFRESH_TOKEN"
            )

        url = f"{settings.zoho_accounts_url}/oauth/v2/token"
        params = {
            "refresh_token": settings.zoho_refresh_token,
            "client_id": settings.zoho_client_id,
            "client_secret": settings.zoho_client_secret,
            "grant_type": "refresh_token",
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                response = await client.post(url, params=params)
            except httpx.HTTPError as exc:
                raise IntegrationError("zoho", f"token refresh network error: {exc}") from exc

        data = _parse(response)
        # Zoho signals auth failures with HTTP 200 and an "error" key, so the
        # body must be inspected even on a 2xx.
        if "error" in data:
            _token_cache.invalidate()
            raise IntegrationError(
                "zoho", f"token refresh rejected: {data['error']}",
                status_code=response.status_code, payload=data,
            )

        token = data.get("access_token")
        if not token:
            raise IntegrationError("zoho", "token refresh returned no access_token",
                                   payload=data)

        _token_cache.set(token, int(data.get("expires_in", 3600)))
        log.info("zoho_token_refreshed", expires_in=data.get("expires_in"))
        return token


def invalidate_token() -> None:
    """Drop the cached token so the next call re-authenticates."""
    _token_cache.invalidate()


def _parse(response: httpx.Response) -> dict:
    try:
        return response.json()
    except ValueError as exc:
        raise IntegrationError(
            "zoho", f"token endpoint returned non-JSON: {response.text[:200]}",
            status_code=response.status_code,
        ) from exc
