"""The one Jinja environment, shared by the admin and the public pages.

Both render from `app/templates`, so they share a single `Jinja2Templates`
rather than each building its own - two environments would silently diverge in
their filters and globals.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.core import locale
from app.core.config import settings

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

# Money, distances and postal codes in the business's own terms, on every
# page: {{ locale.money(order.total) }}, {{ locale.postal_label() }}.
templates.env.globals["locale"] = locale
templates.env.globals["tenant_settings"] = settings
