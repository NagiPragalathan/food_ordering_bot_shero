"""Feedback: comments people left on the dashboard's pages (services/page_comments.py).

The page lists every comment with its totals (Pending, In progress, Done),
copy buttons and status, and every admin login with a Reviewer switch: a
reviewer sees the comment toggle on each page. The /api routes are what the
comment mode on each page talks to. Everything is in Shero US's database,
from either business's pages.
"""

from __future__ import annotations

import uuid
from collections import Counter

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import redirect, render, require_admin
from app.core import tenants
from app.db.models import AdminUser
from app.db.session import get_primary_session
from app.services import page_comments as pc

router = APIRouter(prefix="/feedback", tags=["admin"])

# The tabs: every status, then all of them.
TABS = {**pc.STATUSES, "all": "All"}


def _reviewer(user: AdminUser) -> None:
    if not pc.can_comment(user.email):
        raise HTTPException(status_code=403,
                            detail="You are not a reviewer: switch yourself on, on the Feedback page.")


def _error(exc: pc.CommentError) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=400)


def _id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(status_code=404, detail="No such comment.") from None


@router.get("", name="admin_feedback")
async def feedback_page(
    request: Request,
    status: str = "all",
    business: str = "",
    page: str = "",
    session: AsyncSession = Depends(get_primary_session),
    current_user: AdminUser = Depends(require_admin),
):
    status = status if status in TABS else "all"
    business = business if business in tenants.BY_KEY else ""
    everything = await pc.threads(session)
    counts = Counter(t.root.status for t in everything)
    shown = [t for t in everything
             if (status == "all" or t.root.status == status)
             and (not business or t.root.business == business)
             and (not page or t.root.path == page)]
    # Grouped by page, the pages in the order they were first commented on.
    groups: dict[str, list] = {}
    for found in shown:
        groups.setdefault(found.root.path, []).append(found)
    numbers = {}
    for path in {t.root.path for t in everything}:
        for n, found in enumerate((t for t in everything if t.root.path == path), start=1):
            numbers[found.root.id] = n
    pages = sorted({(t.root.path, pc.page_name(t.root), t.root.business) for t in everything},
                   key=lambda p: (p[2], p[0]))
    reviewing = {r.email for r in await pc.reviewers(session)}
    users = list((await session.execute(
        select(AdminUser).order_by(AdminUser.is_active.desc(), AdminUser.email))).scalars())
    return render(request, "admin/feedback.html", {
        "current_user": current_user,
        "status": status, "business": business, "page_filter": page,
        "tabs": TABS,
        "statuses": pc.STATUSES,
        "totals": {"all": len(everything), **{key: counts.get(key, 0) for key in pc.STATUSES},
                   "pages": len({t.root.path for t in everything})},
        "groups": [{"path": path, "threads": threads, "first": threads[0].root}
                   for path, threads in groups.items()],
        "numbers": numbers,
        "pages": pages,
        "businesses": [(t.key, t.name) for t in tenants.ALL],
        "users": users,
        "reviewing": reviewing,
        "serialize": pc.serialize,
        "page_name": pc.page_name,
        "business_name": pc.business_name,
        "status_label": pc.label,
    })


@router.post("/reviewers", name="admin_feedback_reviewer")
async def set_reviewer(
    request: Request,
    email: str = Form(default=""),
    reviewer: str = Form(default=""),
    session: AsyncSession = Depends(get_primary_session),
    current_user: AdminUser = Depends(require_admin),
):
    """Switch an admin login's Reviewer on or off."""
    url = str(request.url_for("admin_feedback")) + "#reviewers"
    user = (await session.execute(select(AdminUser).where(
        AdminUser.email == email.strip().lower()))).scalar_one_or_none()
    if user is None:
        return redirect(url, flash=("error", "That is not an admin login."))
    if reviewer:
        await pc.add_reviewer(session, user.email, added_by=current_user.email)
        return redirect(url, flash=("success", f"{user.name or user.email} is a reviewer: the "
                                               "comment toggle shows at the top of every page."))
    await pc.remove_reviewer(session, user.email, by=current_user.email)
    return redirect(url, flash=("success", f"{user.name or user.email} is no longer a reviewer."))


@router.post("/comments/{comment_id}/status", name="admin_feedback_status")
async def status_form(
    request: Request,
    comment_id: str,
    status: str = Form(default=pc.DONE),
    session: AsyncSession = Depends(get_primary_session),
    current_user: AdminUser = Depends(require_admin),
):
    back = request.headers.get("referer") or str(request.url_for("admin_feedback"))
    try:
        await pc.set_status(session, _id(comment_id), status, email=current_user.email)
    except pc.CommentError as exc:
        return redirect(back, flash=("error", str(exc)))
    return redirect(back, flash=("success", f"Marked {pc.label(status)}."))


@router.post("/comments/{comment_id}/delete", name="admin_feedback_delete")
async def delete_form(
    request: Request,
    comment_id: str,
    session: AsyncSession = Depends(get_primary_session),
    current_user: AdminUser = Depends(require_admin),
):
    await pc.remove(session, _id(comment_id), email=current_user.email)
    back = request.headers.get("referer") or str(request.url_for("admin_feedback"))
    return redirect(back, flash=("success", "Comment deleted."))


# --- what comment mode on each page calls ------------------------------------------------
@router.get("/api/comments", name="admin_feedback_api_list")
async def api_list(
    path: str,
    session: AsyncSession = Depends(get_primary_session),
    current_user: AdminUser = Depends(require_admin),
):
    return {"threads": [pc.serialize(t) for t in await pc.threads(session, path=path)],
            "can_comment": pc.can_comment(current_user.email)}


@router.post("/api/comments", name="admin_feedback_api_create")
async def api_create(
    request: Request,
    session: AsyncSession = Depends(get_primary_session),
    current_user: AdminUser = Depends(require_admin),
):
    _reviewer(current_user)
    data = await request.json()
    try:
        found = await pc.create(session, data if isinstance(data, dict) else {},
                                email=current_user.email, name=current_user.name)
    except pc.CommentError as exc:
        return _error(exc)
    return {"thread": pc.serialize(found)}


@router.post("/api/comments/{comment_id}/replies", name="admin_feedback_api_reply")
async def api_reply(
    request: Request,
    comment_id: str,
    session: AsyncSession = Depends(get_primary_session),
    current_user: AdminUser = Depends(require_admin),
):
    data = await request.json()
    try:
        found = await pc.reply(session, _id(comment_id), str((data or {}).get("body", "")),
                               email=current_user.email, name=current_user.name)
    except pc.CommentError as exc:
        return _error(exc)
    return {"thread": pc.serialize(found)}


@router.post("/api/comments/{comment_id}/status", name="admin_feedback_api_status")
async def api_status(
    request: Request,
    comment_id: str,
    session: AsyncSession = Depends(get_primary_session),
    current_user: AdminUser = Depends(require_admin),
):
    data = await request.json()
    try:
        found = await pc.set_status(session, _id(comment_id), str((data or {}).get("status", "")),
                                    email=current_user.email)
    except pc.CommentError as exc:
        return _error(exc)
    return {"thread": pc.serialize(found)}


@router.post("/api/comments/{comment_id}/delete", name="admin_feedback_api_delete")
async def api_delete(
    comment_id: str,
    session: AsyncSession = Depends(get_primary_session),
    current_user: AdminUser = Depends(require_admin),
):
    await pc.remove(session, _id(comment_id), email=current_user.email)
    return {"deleted": comment_id}
