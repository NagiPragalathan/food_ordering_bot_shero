"""Menu import: pull from a Google Sheet, or upload a CSV."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import redirect, render, require_admin
from app.core.exceptions import IntegrationError
from app.core.logging import get_logger
from app.db.models import AdminUser, Category, Cuisine, MenuItem
from app.db.session import get_session
from app.services.menu_import import import_from_csv_text, import_from_sheet

log = get_logger(__name__)
router = APIRouter(prefix="/import", tags=["admin"])

MAX_UPLOAD_BYTES = 5 * 1024 * 1024


@router.get("", name="admin_import")
async def import_form(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    cuisines = list((await session.execute(
        select(Cuisine).order_by(Cuisine.position, Cuisine.name)
    )).scalars())

    counts = {}
    for cuisine in cuisines:
        counts[cuisine.slug] = {
            "categories": int(await session.scalar(
                select(func.count(Category.id)).where(
                    Category.cuisine_id == cuisine.id)
            ) or 0),
            "items": int(await session.scalar(
                select(func.count(MenuItem.id))
                .join(Category, MenuItem.category_id == Category.id)
                .where(Category.cuisine_id == cuisine.id)
            ) or 0),
        }

    return render(request, "admin/import.html", {
        "current_user": current_user,
        "cuisines": cuisines,
        "counts": counts,
    })


@router.post("/sheet", name="admin_import_sheet")
async def import_sheet(
    request: Request,
    sheet_url: str = Form(...),
    deactivate_missing: str = Form(default=""),
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    """Import every tab of a Google Sheet."""
    url = str(request.url_for("admin_import"))

    try:
        result = await import_from_sheet(
            session, sheet_url.strip(),
            deactivate_missing=deactivate_missing == "on",
        )
    except ValueError as exc:
        return redirect(url, flash=("error", str(exc)))
    except IntegrationError as exc:
        return redirect(url, flash=("error", exc.message))

    log.info("menu_import_sheet", by=current_user.email,
             summary=result.summary())
    return redirect(url, flash=_flash_for(result))


@router.post("/csv", name="admin_import_csv")
async def import_csv(
    request: Request,
    cuisine: str = Form(default=""),
    deactivate_missing: str = Form(default=""),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    """Import a single uploaded CSV, in the same layout as a sheet tab."""
    url = str(request.url_for("admin_import"))

    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        return redirect(url, flash=(
            "error", f"That file is larger than {MAX_UPLOAD_BYTES // 1024 // 1024} MB."
        ))
    if not raw.strip():
        return redirect(url, flash=("error", "That file is empty."))

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Sheets exported from Excel are often cp1252.
        try:
            text = raw.decode("cp1252")
        except UnicodeDecodeError:
            return redirect(url, flash=(
                "error", "Could not read that file - save it as UTF-8 CSV and retry."
            ))

    result = await import_from_csv_text(
        session, text, cuisine=cuisine.strip(),
        deactivate_missing=deactivate_missing == "on",
    )

    if not result.cuisines:
        return redirect(url, flash=(
            "error",
            "Nothing was imported. Check the file has a cuisine title row and "
            "category headers with 'Description' in the fourth column, or type "
            "the cuisine name in the box before uploading.",
        ))

    log.info("menu_import_csv", by=current_user.email, filename=file.filename,
             summary=result.summary())
    return redirect(url, flash=_flash_for(result))


def _flash_for(result) -> tuple[str, str]:
    """Turn an ImportResult into a message, surfacing any warnings."""
    message = f"Imported {result.summary()}."
    if result.warnings:
        shown = result.warnings[:5]
        message += "\n\nWarnings:\n" + "\n".join(f"• {w}" for w in shown)
        if len(result.warnings) > len(shown):
            message += f"\n• ...and {len(result.warnings) - len(shown)} more"
        return "warning", message
    return "success", message
