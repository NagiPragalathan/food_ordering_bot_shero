"""Retargeting nudges: the page that runs them.

Deliberately not in the sidebar yet - it is reached at /admin/nudges. The
page is a shell; everything on it talks to the JSON API (api/routes/nudges.py)
with the admin's sign-in cookie, so the page and the API can never disagree.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.admin.deps import render, require_admin
from app.db.models import AdminUser

router = APIRouter(prefix="/nudges", tags=["admin"])


@router.get("", name="admin_nudges")
async def nudges_page(request: Request, current_user: AdminUser = Depends(require_admin)):
    return render(request, "admin/nudges.html", {"current_user": current_user})
