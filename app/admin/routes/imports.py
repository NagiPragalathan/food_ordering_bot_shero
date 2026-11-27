"""Menu import: pull from a Google Sheet, or upload a CSV. Also the bulk
price update: PPP (and MRP) of dishes already on the menu, from a sheet."""

from __future__ import annotations

from pathlib import PurePath

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import redirect, render, require_admin
from app.core.exceptions import IntegrationError
from app.core.logging import get_logger
from app.db.models import AdminUser, Category, Cuisine, MenuItem
from app.db.session import get_session
from app.services import erp_import, price_update
from app.services.menu_import import import_from_csv_text, import_from_sheet

log = get_logger(__name__)
router = APIRouter(prefix="/import", tags=["admin"])

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_PRICE_FILE_BYTES = 10 * 1024 * 1024
# The Check step lists this many changes and missing dishes on the page.
SHOWN = 300


@router.get("", name="admin_import")
async def import_form(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    return await _page(request, session, current_user)


async def _page(request: Request, session: AsyncSession, current_user: AdminUser, **extra):
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
        "photos_waiting": await erp_import.photos_waiting(session),
        "shown": SHOWN,
        **extra,
    })


@router.post("/prices", name="admin_import_prices")
async def check_prices(
    request: Request,
    sheet_url: str = Form(default=""),
    file: UploadFile | None = File(default=None),
    update_mrp: str = Form(default=""),
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    """The Check step: what a pricing sheet would change. Writes nothing."""
    url = str(request.url_for("admin_import"))
    plan = price_update.PricePlan()
    try:
        tabs = await _price_tabs(sheet_url.strip(), file)
        rows = price_update.read_tabs(tabs, plan)
    except (ValueError, price_update.PriceSheetError) as exc:
        return redirect(url, flash=("error", str(exc)))
    except IntegrationError as exc:
        return redirect(url, flash=("error", exc.message))
    if not rows:
        return redirect(url, flash=("error", (
            "No prices found. Each tab needs a header row with PPP and MRP (as in the "
            "sample) and the dishes below it.")))
    await price_update.plan(session, rows, update_mrp=update_mrp == "on", plan_=plan)
    log.info("menu_prices_checked", by=current_user.email, summary=plan.summary())
    return await _page(request, session, current_user, prices=plan,
                       payload=price_update.dump_rows(rows, plan))


@router.post("/prices/apply", name="admin_import_prices_apply")
async def apply_prices(
    request: Request,
    payload: str = Form(...),
    update_mrp: str = Form(default=""),
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    """The Update step: the checked sheet's prices, matched again to the menu
    as it is now, then written."""
    url = str(request.url_for("admin_import"))
    plan = price_update.PricePlan()
    try:
        rows = price_update.load_rows(payload, plan)
    except price_update.PriceSheetError as exc:
        return redirect(url, flash=("error", str(exc)))
    await price_update.plan(session, rows, update_mrp=update_mrp == "on", plan_=plan)
    changed = await price_update.apply(session, plan)
    log.info("menu_prices_applied", by=current_user.email, changed=changed,
             summary=plan.summary())
    message = f"Prices updated: {plan.summary()}."
    if plan.not_found:
        message += (" The dishes not found were left alone: check their names on the "
                    "sheet against the menu.")
    return redirect(url, flash=("success" if not plan.not_found else "warning", message))


async def _price_tabs(sheet_url: str, file: UploadFile | None):
    """The pricing sheet's tabs: an uploaded .xlsx or .csv, or a Google Sheet."""
    if file is not None and file.filename:
        data = await file.read()
        if len(data) > MAX_PRICE_FILE_BYTES:
            raise price_update.PriceSheetError(
                f"That file is larger than {MAX_PRICE_FILE_BYTES // 1024 // 1024} MB.")
        name = file.filename.lower()
        if name.endswith(".xlsx"):
            return price_update.workbook_tabs(data)
        if not name.endswith(".csv"):
            raise price_update.PriceSheetError("Choose an Excel (.xlsx) or CSV file.")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
        return [(PurePath(file.filename).stem, price_update.csv_rows(text))]
    if sheet_url:
        return await price_update.sheet_tabs(sheet_url)
    raise price_update.PriceSheetError("Paste the Google Sheet link or choose a file.")


@router.post("/erp", name="admin_import_erp")
async def import_erp(
    request: Request,
    files: list[UploadFile] = File(...),
    cuisine: str = Form(default=""),
    replace: str = Form(default=""),
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    """Import the ERP's item exports, one cuisine per file (services/erp_import.py)."""
    url = str(request.url_for("admin_import"))
    uploads = [f for f in files if f.filename]
    if not uploads:
        return redirect(url, flash=("error", "Choose at least one Excel file."))
    if cuisine.strip() and len(uploads) > 1:
        return redirect(url, flash=("error", "Type a cuisine name only when uploading one "
                                             "file; with several, each is named after its file."))
    batch = []
    for upload in uploads:
        batch.append((upload.filename, await upload.read(), cuisine.strip()))
    try:
        result = await erp_import.import_exports(session, batch, replace=replace == "on")
    except erp_import.ErpImportError as exc:
        return redirect(url, flash=("error", str(exc)))
    log.info("menu_import_erp", by=current_user.email, files=[u.filename for u in uploads],
             replace=replace == "on", summary=result.summary())
    return redirect(url, flash=_flash_for(result))


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
