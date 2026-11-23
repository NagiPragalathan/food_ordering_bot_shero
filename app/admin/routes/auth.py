"""Admin sign in and sign out."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.admin.auth import SESSION_COOKIE, authenticate, issue_session
from app.admin.deps import redirect, render
from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import get_primary_session
from fastapi import Depends

log = get_logger(__name__)
router = APIRouter(tags=["admin"])


def safe_next(value: str | None) -> str | None:
    """The admin page to return to after signing in: a path on this site
    under /admin (or /dst/admin), never another site or the login itself."""
    value = (value or "").strip()
    if (not value.startswith("/") or value.startswith("//") or len(value) > 500
            or any(ch in value for ch in "\\\r\n")):
        return None
    path = value.split("?", 1)[0].rstrip("/")
    if "/admin" not in path or path.endswith(("/login", "/logout")):
        return None
    return value


def login_url(request: Request, next_page: str | None) -> str:
    url = str(request.url_for("admin_login_form"))
    page = safe_next(next_page)
    return f"{url}?{urlencode({'next': page})}" if page else url


@router.get("/login", name="admin_login_form")
async def login_form(request: Request, next: str = ""):
    return render(request, "admin/login.html", {"current_user": None, "next": safe_next(next)})


@router.post("/login", name="admin_login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
    # One login for both businesses, kept in Shero US's database.
    session: AsyncSession = Depends(get_primary_session),
):
    user = await authenticate(session, email, password)
    if user is None:
        # One message for both wrong-email and wrong-password, so the form
        # never reveals which accounts exist.
        return render(
            request,
            "admin/login.html",
            {"error": "Incorrect email or password.", "email": email,
             "current_user": None, "next": safe_next(next)},
            status_code=401,
        )

    # Back to the page that asked for the login (e.g. /admin/sft), or the dashboard.
    response = redirect(safe_next(next) or str(request.url_for("admin_dashboard")))
    _set_session_cookie(response, issue_session(user))
    return response


@router.get("/logout", name="admin_logout")
async def logout(request: Request, next: str = ""):
    response = redirect(login_url(request, next), flash=("success", "Signed out."))
    response.delete_cookie(SESSION_COOKIE)
    return response


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.admin_session_hours * 3600,
        httponly=True,          # not readable from JavaScript
        samesite="lax",         # survives the post-login redirect, blocks CSRF
        secure=settings.is_production,
    )
