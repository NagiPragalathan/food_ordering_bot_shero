"""The brochure PDF the bot sends: the one shipped with the app, or one the
team uploaded on the SFT Settings page.

An upload is kept in the media folder (data/media/sft/, on the server's data
volume, so a redeploy keeps it) under a folder named after its contents:

    /media/sft/<hash>/Shero-Food-Training-SFT-Brochure.pdf

A new file gets a new link, so WhatsApp (which caches a link's file for a
while) never sends the old one, and the name WhatsApp shows stays tidy. The
previous upload is removed once the new one is saved.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from app.core.logging import get_logger
from app.services import media

log = get_logger(__name__)

# Gallabox's limit for a file sent through its API.
MAX_BYTES = 16 * 1024 * 1024
FILENAME = "Shero-Food-Training-SFT-Brochure.pdf"
BUNDLED = Path(__file__).resolve().parents[1] / "static" / "sft" / "shero-food-training-brochure.pdf"


class BrochureError(ValueError):
    """An upload that can't be the brochure (not a PDF, empty, too big)."""


def folder() -> Path:
    return media.MEDIA_DIR / "sft"


def store(data: bytes) -> str:
    """Save an uploaded PDF; returns its name for the config (brochure_file)."""
    if not data:
        raise BrochureError("Choose a PDF file first.")
    if len(data) > MAX_BYTES:
        raise BrochureError(f"That PDF is {len(data) / 1024 / 1024:.1f} MB. WhatsApp files sent "
                            "this way can be 16 MB at most: please make it smaller.")
    if not data.lstrip()[:5] == b"%PDF-":
        raise BrochureError("That file isn't a PDF. Please upload the brochure as a .pdf file.")
    digest = hashlib.sha256(data).hexdigest()[:16]
    target = folder() / digest / FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    log.info("sft_brochure_uploaded", size=len(data), name=f"{digest}/{FILENAME}")
    return f"{digest}/{FILENAME}"


def path_of(name: str) -> Path | None:
    """The uploaded file `name` (as kept in the config), when it is there."""
    if not name:
        return None
    root = folder().resolve()
    candidate = (root / name).resolve()
    if root not in candidate.parents or not candidate.is_file():
        return None
    return candidate


def remove(name: str) -> None:
    """Delete an earlier upload (its folder too). Never anything else."""
    found = path_of(name)
    if found is None:
        return
    try:
        shutil.rmtree(found.parent)
    except OSError as exc:  # a leftover file is harmless
        log.warning("sft_brochure_remove_failed", error=str(exc))


def describe(cfg) -> dict:
    """What the Settings page shows about the brochure in use."""
    if cfg.brochure_url:
        return {"kind": "link", "label": "A link to another site", "size": "",
                "link": cfg.brochure_url}
    uploaded = path_of(cfg.brochure_file)
    if uploaded is not None:
        return {"kind": "upload", "label": cfg.brochure_label or FILENAME,
                "size": _size(uploaded.stat().st_size), "link": cfg.brochure_link()}
    return {"kind": "bundled", "label": "The original SFT brochure",
            "size": _size(BUNDLED.stat().st_size) if BUNDLED.exists() else "",
            "link": cfg.brochure_link(), "missing_upload": bool(cfg.brochure_file)}


def _size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{max(1, n // 1024)} KB"
