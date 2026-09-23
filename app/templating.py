"""The one Jinja environment, shared by the admin and the public pages.

Both render from `app/templates`, so they share a single `Jinja2Templates`
rather than each building its own - two environments would silently diverge in
their filters and globals.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
