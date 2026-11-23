"""The pages' styles come from our own built stylesheets (tailwind/), never
from the Tailwind CDN: when that script was slow or blocked (mobile data,
WhatsApp's in-app browser) the menu and the dashboard showed up unstyled."""

from __future__ import annotations

import re

from app.templating import STATIC_DIR, TEMPLATE_DIR, static_url

PAGES = {
    "admin/base.html": "css/admin.css",
    "admin/login.html": "css/admin.css",
    "admin/sft/base.html": "css/admin.css",
    "order/index.html": "css/order.css",
    "order/location.html": "css/order.css",
    "order/expired.html": "css/order.css",
    "order/sft.html": "css/order.css",
}


def test_no_template_loads_tailwind_from_a_cdn():
    for template in TEMPLATE_DIR.rglob("*.html"):
        source = template.read_text(encoding="utf-8")
        assert "cdn.tailwindcss.com" not in source, template
        assert "tailwind.config" not in source, template


def test_every_full_page_links_its_built_stylesheet():
    for template, css in PAGES.items():
        source = (TEMPLATE_DIR / template).read_text(encoding="utf-8")
        assert f"static_url('{css}')" in source, template
    # Every other admin page gets admin.css from the base layout.
    for template in (TEMPLATE_DIR / "admin").glob("*.html"):
        if template.name.startswith("_") or template.name in {"base.html", "login.html"}:
            continue
        assert '{% extends "admin/base.html" %}' in template.read_text(encoding="utf-8"), template


def test_the_stylesheets_carry_the_brand():
    admin = (STATIC_DIR / "css/admin.css").read_text(encoding="utf-8")
    order = (STATIC_DIR / "css/order.css").read_text(encoding="utf-8")
    for css in (admin, order):
        assert ".bg-brand-500{" in css and "2 155 153" in css      # the logo teal
        assert ".bg-mist-100{" in css
    assert "237 244 243" in admin          # admin's teal-tinted stone-100
    assert ".text-herb-600{" in order      # veg / success green


def test_a_stylesheet_url_changes_with_its_content():
    url = static_url("css/order.css")
    assert re.fullmatch(r"/static/css/order\.css\?v=[0-9a-f]{12}", url)
    assert static_url("css/admin.css") != url
