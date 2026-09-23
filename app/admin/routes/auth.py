"""Admin sign in and sign out."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.admin.auth import SESSION_COOKIE, authenticate, issue_session
from app.admin.deps import redirect, render
from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import get_session
from fastapi import Depends

log = get_logger(__name__)
router = APIRouter(tags=["admin"])


@router.get("/login", name="admin_login_form")
async def login_form(request: Request):
    return render(request, "admin/login.html", {"current_user": None})


@router.post("/login", name="admin_login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    user = await authenticate(session, email, password)
    if user is None:
        # One message for both wrong-email and wrong-password, so the form
        # never reveals which accounts exist.
        return render(
            request,
            "admin/login.html",
            {"error": "Incorrect email or password.", "email": email,
             "current_user": None},
            status_code=401,
        )

    response = redirect(str(request.url_for("admin_dashboard")))
    _set_session_cookie(response, issue_session(user))
    return response


@router.get("/logout", name="admin_logout")
async def logout(request: Request):
    response = redirect(str(request.url_for("admin_login_form")),
                        flash=("success", "Signed out."))
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
