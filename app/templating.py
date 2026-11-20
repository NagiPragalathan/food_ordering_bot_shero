"""The one Jinja environment, shared by the admin and the public pages.

Both render from `app/templates`, so they share a single `Jinja2Templates`
rather than each building its own - two environments would silently diverge in
their filters and globals.
"""

from __future__ import annotations

import hashlib
from functools import cache
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.core import locale
from app.core.config import settings

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


@cache
def static_url(path: str) -> str:
    """`/static/<path>?v=<digest of the file>`: a new file after a deploy is a
    new URL, so no phone keeps showing yesterday's stylesheet."""
    digest = hashlib.sha256((STATIC_DIR / path).read_bytes()).hexdigest()[:12]
    return f"/static/{path}?v={digest}"


# Money, distances and postal codes in the business's own terms, on every
# page: {{ locale.money(order.total) }}, {{ locale.postal_label() }}.
templates.env.globals["locale"] = locale
templates.env.globals["tenant_settings"] = settings
templates.env.globals["static_url"] = static_url
