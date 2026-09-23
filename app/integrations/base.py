"""Shared HTTP plumbing for every upstream integration.

One place for: connection reuse, timeouts, bounded retry on transient
failures, and turning any non-2xx into a typed `IntegrationError`. Individual
clients then only describe *what* they call, not *how*.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.core.exceptions import IntegrationError
from app.core.logging import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, IntegrationError):
        return exc.is_retryable
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))


class ApiClient:
    """Thin async wrapper around `httpx.AsyncClient`.

    The underlying client is created lazily and reused for the process
    lifetime so connections are pooled across webhook invocations.
    """

    service: str = "api"
    base_url: str = ""

    def __init__(self, base_url: str | None = None, timeout: httpx.Timeout | None = None):
        if base_url is not None:
            self.base_url = base_url
        self._timeout = timeout or DEFAULT_TIMEOUT
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            async with self._lock:
                if self._client is None or self._client.is_closed:
                    self._client = httpx.AsyncClient(
                        base_url=self.base_url, timeout=self._timeout
                    )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def default_headers(self) -> dict[str, str]:
        """Per-request auth headers. Subclasses override (may refresh tokens)."""
        return {}

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        reraise=True,
    )
    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json: Any | None = None,
        data: Any | None = None,
        headers: dict[str, str] | None = None,
        expected: tuple[int, ...] = (200, 201, 202, 204),
    ) -> Any:
        client = await self._get_client()
        merged = {**await self.default_headers(), **(headers or {})}

        started = time.perf_counter()
        try:
            response = await client.request(
                method, url, params=params, json=json, data=data, headers=merged
            )
        except httpx.HTTPError as exc:
            log.warning("upstream_network_error", service=self.service,
                        method=method, url=url, error=str(exc))
            raise IntegrationError(self.service, f"network error calling {url}: {exc}") from exc

        elapsed_ms = round((time.perf_counter() - started) * 1000)
        log.info("upstream_call", service=self.service, method=method, url=url,
                 status=response.status_code, ms=elapsed_ms)

        if response.status_code not in expected:
            raise IntegrationError(
                self.service,
                f"{method} {url} returned {response.status_code}: {response.text[:500]}",
                status_code=response.status_code,
                payload=_safe_json(response),
            )

        if response.status_code == 204 or not response.content:
            return None
        return _safe_json(response)

    async def get(self, url: str, **kw) -> Any:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw) -> Any:
        return await self.request("POST", url, **kw)

    async def put(self, url: str, **kw) -> Any:
        return await self.request("PUT", url, **kw)


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text[:2000]}


class TokenCache:
    """In-memory OAuth access-token cache with an expiry safety margin.

    Zoho and Uber both hand out short-lived access tokens from a client
    id/secret pair. Refreshing on every call would burn the rate limit, so the
    token is held until shortly before it actually expires.
    """

    def __init__(self, leeway_seconds: int = 60) -> None:
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._leeway = leeway_seconds
        self._lock = asyncio.Lock()

    @property
    def is_valid(self) -> bool:
        return bool(self._token) and time.time() < (self._expires_at - self._leeway)

    def set(self, token: str, expires_in: int) -> None:
        self._token = token
        self._expires_at = time.time() + expires_in

    def get(self) -> str | None:
        return self._token if self.is_valid else None

    def invalidate(self) -> None:
        self._token = None
        self._expires_at = 0.0

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock
