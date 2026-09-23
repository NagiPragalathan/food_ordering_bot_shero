"""Uber Direct OAuth - client-credentials grant.

Takes the Client ID / Client Secret from the client's Uber Direct developer
account and exchanges them for a short-lived bearer token (30 days nominally,
but treated as expiring per the `expires_in` the API returns).
"""

from __future__ import annotations

import httpx

from app.core.config import settings
from app.core.exceptions import ConfigurationError, IntegrationError
from app.core.logging import get_logger
from app.integrations.base import TokenCache

log = get_logger(__name__)

_token_cache = TokenCache(leeway_seconds=300)


async def get_access_token(*, force_refresh: bool = False) -> str:
    if not force_refresh:
        cached = _token_cache.get()
        if cached:
            return cached

    async with _token_cache.lock:
        if not force_refresh:
            cached = _token_cache.get()
            if cached:
                return cached

        if not (settings.uber_client_id and settings.uber_client_secret):
            raise ConfigurationError(
                "Uber credentials missing: set UBER_CLIENT_ID and UBER_CLIENT_SECRET"
            )

        # This endpoint is form-encoded, not JSON.
        form = {
            "client_id": settings.uber_client_id,
            "client_secret": settings.uber_client_secret,
            "grant_type": "client_credentials",
            "scope": settings.uber_scope,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                response = await client.post(settings.uber_auth_url, data=form)
            except httpx.HTTPError as exc:
                raise IntegrationError("uber", f"token network error: {exc}") from exc

        if response.status_code != 200:
            _token_cache.invalidate()
            raise IntegrationError(
                "uber", f"token request failed: {response.text[:300]}",
                status_code=response.status_code,
            )

        data = response.json()
        token = data.get("access_token")
        if not token:
            raise IntegrationError("uber", "token response had no access_token", payload=data)

        _token_cache.set(token, int(data.get("expires_in", 2592000)))
        log.info("uber_token_refreshed", expires_in=data.get("expires_in"))
        return token


def invalidate_token() -> None:
    _token_cache.invalidate()
