"""Shared admin plumbing: templates, flash messages, the auth guard."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import Depends, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import templating
from app.admin.auth import SESSION_COOKIE, get_user, read_session
from app.core import tenants
from app.core.config import settings
from app.db.models import AdminUser
from app.db.session import get_primary_session
from app.services import allowlist, page_comments, reply_triggers

# Shared with the public ordering pages - see app/templating.py.
TEMPLATE_DIR = templating.TEMPLATE_DIR
templates = templating.templates

FLASH_COOKIE = "shero_flash"


class NotAuthenticated(Exception):
    """Raised by `require_admin`; the app turns it into a redirect to login."""


async def require_admin(
    request: Request,
    session: AsyncSession = Depends(get_primary_session),
) -> AdminUser:
    """Resolve the signed session cookie to a live, active admin user.

    One login for both businesses: the users are in Shero US's database,
    whichever business's pages are open."""
    payload = read_session(request.cookies.get(SESSION_COOKIE))
    if not payload:
        raise NotAuthenticated

    user = await get_user(session, payload.get("id", ""))
    if user is None or not user.is_active:
        raise NotAuthenticated
    # Who sees the comment toggle (the Feedback page's reviewers).
    await page_comments.refresh_reviewers(session)
    request.state.admin_user = user
    return user


def render(request: Request, template: str, context: dict[str, Any] | None = None,
           *, status_code: int = 200):
    """Render a template with the shared context, consuming any flash message."""
    payload = {
        "request": request,
        "flash": _read_flash(request),
        "settings": settings,
        # For the "test mode" badge in every page's header ("Bot off" when
        # the bot answers nobody, as on a new DST India number).
        "bot_restricted": allowlist.is_restricted(),
        "bot_off": allowlist.is_off(),
        # ...and the "keywords only" one.
        "bot_keyword_mode": reply_triggers.is_keyword_mode(),
        # The business tabs at the top of every page.
        "tenant": tenants.current(),
        "tenant_tabs": tenant_tabs(request),
        **(context or {}),
    }
    payload["page_comments"] = _comment_mode(request, template, payload.get("current_user"))
    response = templates.TemplateResponse(
        request=request, name=template, context=payload, status_code=status_code
    )
    # A flash is shown once.
    if request.cookies.get(FLASH_COOKIE):
        response.delete_cookie(FLASH_COOKIE)
    return response


def _comment_mode(request: Request, template: str, user) -> dict[str, Any]:
    """What the page's comment mode needs (static/admin/page-comments.js):
    whether this admin may comment, and what draws the page."""
    user = user or getattr(request.state, "admin_user", None)
    email = getattr(user, "email", "") or ""
    return {
        "can_comment": page_comments.can_comment(email),
        "me": {"email": email, "name": getattr(user, "name", None) or email},
        "business": tenants.current().key,
        **page_comments.source_of(request, template),
    }


def tenant_tabs(request: Request) -> list[dict[str, Any]]:
    """One tab per business: its name, where it leads, and whether it is open.

    A tab keeps you in the same section of the other business (Orders ->
    Orders), since every business has every page; not on one record, which
    belongs to one business only."""
    here = tenants.current()
    root = request.scope.get("root_path", "")
    page = request.url.path
    # The page's path without the current business's prefix: /dst/admin/x -> /admin/x.
    inner = page[len(root):] if root and page.startswith(root) else page
    parts = [p for p in inner.split("/") if p]
    inner = "/admin/" + (parts[1] if len(parts) > 1 and parts[0] == "admin" else "")
    outer = root[: len(root) - len(here.url_prefix)] if here.url_prefix else root
    # Shero Food Training (app/sft/admin.py) is a tab of its own: Shero US's
    # course bot, at /admin/sft. From it, a business tab opens its dashboard.
    on_sft = inner == "/admin/sft"
    if on_sft:
        inner = "/admin/"
    tabs = []
    for tenant in tenants.enabled():
        tabs.append({"key": tenant.key, "name": tenant.name, "active": tenant == here and not on_sft,
                     "url": f"{outer}{tenant.url_prefix}{inner}"})
    tabs.append({"key": "sft", "name": "Shero Food Training", "active": on_sft,
                 "url": f"{outer}{tenants.DEFAULT.url_prefix}/admin/sft"})
    return tabs


def redirect(url: str, *, flash: tuple[str, str] | None = None) -> RedirectResponse:
    """303 redirect after a form post, optionally carrying a flash message."""
    response = RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
    if flash:
        category, message = flash
        response.set_cookie(
            FLASH_COOKIE,
            json.dumps([[category, message]]),
            max_age=30,
            httponly=True,
            samesite="lax",
            secure=settings.is_production,
        )
    return response


def _read_flash(request: Request) -> list[tuple[str, str]]:
    raw = request.cookies.get(FLASH_COOKIE)
    if not raw:
        return []
    try:
        loaded = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(loaded, list):
        return []
    return [(str(c), str(m)) for c, m in loaded if isinstance(m, str)][:3]
