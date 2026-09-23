"""Dish images: pull them off the sheet once, then serve them ourselves.

The sheet's IMAGE column holds links that belong to somebody else - a Google
Drive file, an `=IMAGE("…")` formula, a supplier's CDN. Hot-linking those into
the ordering page means every customer's page load depends on a third party
staying up, staying public, and not rate-limiting us.

So an import downloads each image once into `data/media/` and stores the local
path on the dish. After that the storefront serves its own files.

Nothing here raises: an image that cannot be fetched leaves the dish without
one, and the page draws its placeholder. A missing photo must never fail a
menu import.
"""

from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path

import httpx

from app.core.logging import get_logger

log = get_logger(__name__)

MEDIA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "media"
MEDIA_URL_PREFIX = "/media"

# Enough for a menu photo; anything larger is a mistake rather than a dish.
MAX_BYTES = 4 * 1024 * 1024
TIMEOUT_SECONDS = 15

# Several image hosts (Wikimedia among them) reject requests that send no
# User-Agent with a bare 403, so identify ourselves.
HEADERS = {
    "User-Agent": "SheroOrderingBot/1.0 (+menu image import)",
    "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8",
}

EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

# `=IMAGE("https://…")`, the formula Google Sheets exports as an empty cell in
# some views and as raw text in others.
_FORMULA = re.compile(r'=\s*IMAGE\s*\(\s*["\']([^"\']+)["\']', re.IGNORECASE)
_DRIVE_ID = re.compile(r"/file/d/([A-Za-z0-9_-]{10,})|[?&]id=([A-Za-z0-9_-]{10,})")


def normalise_source(cell: str) -> str:
    """Turn whatever is in the IMAGE cell into a fetchable URL.

    Handles a bare URL, an `=IMAGE("…")` formula, and a Google Drive share
    link (which serves an HTML preview page, not the image, unless it is
    rewritten to the direct-download form).
    """
    value = (cell or "").strip()
    if not value:
        return ""

    formula = _FORMULA.search(value)
    if formula:
        value = formula.group(1).strip()

    if not value.lower().startswith(("http://", "https://")):
        return ""

    if "drive.google.com" in value:
        match = _DRIVE_ID.search(value)
        if match:
            file_id = match.group(1) or match.group(2)
            return f"https://drive.google.com/uc?export=download&id={file_id}"

    return value


def local_path_for(url: str, content_type: str) -> Path:
    """A stable filename, so re-importing overwrites rather than accumulates."""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return MEDIA_DIR / f"{digest}{EXTENSIONS.get(content_type, '.jpg')}"


async def fetch(url: str, *, client: httpx.AsyncClient | None = None) -> str | None:
    """Download one image and return the path to serve it at, or None.

    Returns the existing file without re-downloading when the URL has been
    fetched before, so a re-import of 287 rows is cheap.
    """
    source = normalise_source(url)
    if not source:
        return None

    # A URL already pointing at our own store needs no work.
    if source.startswith(MEDIA_URL_PREFIX):
        return source

    for extension in EXTENSIONS.values():
        cached = MEDIA_DIR / (hashlib.sha1(source.encode()).hexdigest()[:16] + extension)
        if cached.exists():
            return f"{MEDIA_URL_PREFIX}/{cached.name}"

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS,
                                         follow_redirects=True,
                                         headers=HEADERS)
    try:
        response = await client.get(source)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("image_fetch_failed", url=source[:120], error=str(exc))
        return None
    finally:
        if owns_client:
            await client.aclose()

    content_type = response.headers.get("content-type", "").split(";")[0].strip()
    if content_type not in EXTENSIONS:
        # Usually a Drive HTML interstitial rather than the file itself.
        log.warning("image_not_an_image", url=source[:120], content_type=content_type)
        return None

    if len(response.content) > MAX_BYTES:
        log.warning("image_too_large", url=source[:120], bytes=len(response.content))
        return None

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    destination = local_path_for(source, content_type)
    try:
        destination.write_bytes(response.content)
    except OSError as exc:
        log.error("image_write_failed", path=str(destination), error=str(exc))
        return None

    log.info("image_stored", name=destination.name, bytes=len(response.content))
    return f"{MEDIA_URL_PREFIX}/{destination.name}"


# A menu thumbnail never needs more than this; the sheet's originals are
# ~850KB PNGs, which would make a phone menu unusable.
THUMBNAIL_MAX_PX = 800
JPEG_QUALITY = 82


def store_bytes(data: bytes, *, key: str) -> str | None:
    """Save an image we already hold (e.g. lifted out of an XLSX).

    `key` identifies the source so the same picture keeps the same filename
    across re-imports. Returns the path to serve it at, or None.
    """
    if not data:
        return None

    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    destination = MEDIA_DIR / f"{digest}.jpg"

    try:
        shrunk = _shrink(data)
    except Exception as exc:            # Pillow raises a family of errors
        log.warning("image_unreadable", key=key[:80], error=str(exc))
        return None

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        destination.write_bytes(shrunk)
    except OSError as exc:
        log.error("image_write_failed", path=str(destination), error=str(exc))
        return None

    return f"{MEDIA_URL_PREFIX}/{destination.name}"


def _shrink(data: bytes) -> bytes:
    """Downscale to a sane thumbnail and re-encode as JPEG."""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        # Flatten transparency onto white; a JPEG cannot carry an alpha
        # channel and would otherwise render it as black.
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGBA")
            flat = Image.new("RGB", image.size, (255, 255, 255))
            flat.paste(image, mask=image.split()[-1])
            image = flat
        else:
            image = image.convert("RGB")

        image.thumbnail((THUMBNAIL_MAX_PX, THUMBNAIL_MAX_PX), Image.LANCZOS)

        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return buffer.getvalue()


async def fetch_many(urls: list[str]) -> dict[str, str]:
    """Fetch a batch over one connection pool. Failures are simply absent."""
    results: dict[str, str] = {}
    sources = [u for u in dict.fromkeys(urls) if u]
    if not sources:
        return results

    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS,
                                 follow_redirects=True,
                                 headers=HEADERS) as client:
        for url in sources:
            stored = await fetch(url, client=client)
            if stored:
                results[url] = stored
    return results
