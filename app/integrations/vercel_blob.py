"""Vercel Blob: where dish photos live when the bot runs on Vercel.

A Vercel Function cannot keep files (only /tmp, which disappears), so photos
are uploaded here and the dish stores the public Blob URL instead of a
/media/... path. Used only when BLOB_READ_WRITE_TOKEN is set, which Vercel
does by itself once a Blob store is connected to the project.

The request mirrors the official @vercel/blob SDK's put(): a PUT to
https://vercel.com/api/blob/?pathname=... with the read-write token, the
store id (part of the token) and the API version it speaks.
"""

from __future__ import annotations

import httpx

from app.core.config import is_unset, settings
from app.core.exceptions import IntegrationError
from app.core.logging import get_logger

log = get_logger(__name__)

API_URL = "https://vercel.com/api/blob/"
API_VERSION = "12"
TIMEOUT_SECONDS = 30
ATTEMPTS = 3
# A year: a photo's address changes whenever the photo does (it is named by a
# digest), so it never needs revalidating.
CACHE_MAX_AGE = str(365 * 24 * 3600)


def is_configured() -> bool:
    return not is_unset(settings.blob_read_write_token)


def _store_id(token: str) -> str:
    """vercel_blob_rw_<storeId>_<secret> -> <storeId>."""
    parts = token.split("_")
    return parts[3] if len(parts) > 4 else ""


def put(pathname: str, data: bytes, content_type: str) -> str:
    """Upload (or replace) one public file and return its URL.

    Synchronous on purpose: the photo writers (menu import, the admin's photo
    upload) are synchronous, and each upload is a single short request.
    Raises IntegrationError when the store refuses or cannot be reached.
    """
    token = settings.blob_read_write_token.strip()
    headers = {
        "authorization": f"Bearer {token}",
        "x-api-version": API_VERSION,
        "x-vercel-blob-store-id": _store_id(token),
        "x-vercel-blob-access": "public",
        "x-content-type": content_type,
        # Keep the exact name, and replace it on re-import, so a dish's photo
        # address is stable.
        "x-add-random-suffix": "0",
        "x-allow-overwrite": "1",
        "x-cache-control-max-age": CACHE_MAX_AGE,
    }
    last_error = "no attempt made"
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = httpx.put(API_URL, params={"pathname": pathname}, content=data,
                                 headers=headers, timeout=TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:
            last_error = str(exc)
            log.warning("blob_put_retry", pathname=pathname, attempt=attempt, error=last_error)
            continue
        if response.status_code < 300:
            url = (response.json() or {}).get("url")
            if url:
                return url
            last_error = "no url in the response"
            break
        last_error = f"{response.status_code} {response.text[:200]}"
        if response.status_code < 500 and response.status_code != 429:
            break                       # a refusal, not an outage: retrying will not help
        log.warning("blob_put_retry", pathname=pathname, attempt=attempt, error=last_error)
    raise IntegrationError("vercel_blob", f"upload of {pathname} failed: {last_error}")
