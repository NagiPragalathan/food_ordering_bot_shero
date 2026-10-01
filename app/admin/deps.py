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
from app.core.config import settings
from app.db.models import AdminUser
from app.db.session import get_session
from app.services import allowlist

# Shared with the public ordering pages - see app/templating.py.
TEMPLATE_DIR = templating.TEMPLATE_DIR
templates = templating.templates

FLASH_COOKIE = "shero_flash"


class NotAuthenticated(Exception):
    """Raised by `require_admin`; the app turns it into a redirect to login."""


async def require_admin(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> AdminUser:
    """Resolve the signed session cookie to a live, active admin user."""
    payload = read_session(request.cookies.get(SESSION_COOKIE))
    if not payload:
        raise NotAuthenticated

    user = await get_user(session, payload.get("id", ""))
    if user is None or not user.is_active:
        raise NotAuthenticated
    return user


def render(request: Request, template: str, context: dict[str, Any] | None = None,
           *, status_code: int = 200):
    """Render a template with the shared context, consuming any flash message."""
    payload = {
        "request": request,
        "flash": _read_flash(request),
        "settings": settings,
        # For the "test mode" badge in every page's header.
        "bot_restricted": allowlist.is_restricted(),
        **(context or {}),
    }
    response = templates.TemplateResponse(
        request=request, name=template, context=payload, status_code=status_code
    )
    # A flash is shown once.
    if request.cookies.get(FLASH_COOKIE):
        response.delete_cookie(FLASH_COOKIE)
    return response


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
