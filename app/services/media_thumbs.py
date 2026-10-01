"""Small copies of dish photos for the menu cards and the cart.

A card shows its photo at 80x80 CSS pixels, but the stored photo is up to
800 px (about 90 KB). Across a 287-dish menu that is ~26 MB for a phone to
pull, which over a slow link is the difference between a page that loads and
one that does not. A 240 px thumbnail (3x the card, for sharp phone screens)
is around a tenth of the size.

Thumbnails are made on first request and kept on disk next to the photos.
A thumbnail that cannot be made is not an error for the customer: the
original photo is served instead. Photos in Vercel Blob have their thumbnail
uploaded beside them when stored (media.save), at media/thumbs/<stem>.jpg.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.core.logging import get_logger
from app.services import media

log = get_logger(__name__)

THUMB_DIR = media.MEDIA_DIR / "thumbs"
THUMB_URL_PREFIX = f"{media.MEDIA_URL_PREFIX}/thumb"
THUMB_PX = media.THUMB_PX
THUMB_QUALITY = media.THUMB_QUALITY

# Photo files are named by a 16-hex digest (see media.py). Anything else is
# refused, which also keeps a request from naming a path outside MEDIA_DIR.
_NAME = re.compile(r"^[0-9a-f]{16}\.(?:jpg|jpeg|png|webp|gif)$")
# A photo in Vercel Blob: https://<store>.public.blob.vercel-storage.com/media/<name>
_BLOB = re.compile(r"^(https://[a-z0-9-]+\.public\.blob\.vercel-storage\.com/media/)"
                   r"([0-9a-f]{16})\.(?:jpg|jpeg|png|webp|gif)$")


def thumb_url(image_url: str | None) -> str:
    """The card-sized URL for a dish photo; other URLs pass through."""
    url = image_url or ""
    blob = _BLOB.match(url)
    if blob:
        return f"{blob.group(1)}thumbs/{blob.group(2)}.jpg"
    prefix = f"{media.MEDIA_URL_PREFIX}/"
    name = url[len(prefix):] if url.startswith(prefix) else ""
    return f"{THUMB_URL_PREFIX}/{name}" if _NAME.match(name) else url


def thumbnail_file(name: str) -> Path | None:
    """Path of the thumbnail for one photo, making it if needed.

    None when there is no such photo. Blocking (Pillow), so call it off the
    event loop.
    """
    if not _NAME.match(name):
        return None
    source = media.MEDIA_DIR / name
    if not source.is_file():
        return None

    target = THUMB_DIR / f"{source.stem}.jpg"
    if target.is_file() and target.stat().st_mtime >= source.stat().st_mtime:
        return target

    try:
        data = media._shrink(source.read_bytes(), max_px=THUMB_PX, quality=THUMB_QUALITY)
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    except Exception as exc:   # Pillow or disk: serve the full photo instead
        log.warning("thumbnail_failed", name=name, error=str(exc))
        return source
    return target
