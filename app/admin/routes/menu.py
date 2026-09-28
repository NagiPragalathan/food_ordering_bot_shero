"""Menu browser, and adding/editing/removing menu rows by hand.

The routes stay thin on purpose: every rule about what a valid dish is lives
in `services/menu_admin.py`, which the importer's conventions are shared with.
Here we only read the form, call it, and turn a `MenuEditError` into a flash
message on the page the person came from.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.admin.deps import redirect, render, require_admin
from app.core.logging import get_logger
from app.db.models import AdminUser, Category, Cuisine, MenuItem
from app.db.session import get_session
from app.services import menu_admin
from app.services.menu_admin import MenuEditError

log = get_logger(__name__)
router = APIRouter(prefix="/menu", tags=["admin"])


@router.get("", name="admin_menu")
async def menu_index(
    request: Request,
    cuisine: str | None = None,
    q: str | None = None,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    """Browse the menu, filtered by cuisine and/or a search term."""
    cuisines = list((await session.execute(
        select(Cuisine).order_by(Cuisine.position, Cuisine.name)
    )).scalars())

    selected = None
    if cuisine:
        selected = next((c for c in cuisines if c.slug == cuisine), None)
    if selected is None and cuisines and not q:
        selected = cuisines[0]

    query = (
        select(MenuItem)
        .options(selectinload(MenuItem.category).selectinload(Category.cuisine))
        .join(Category, MenuItem.category_id == Category.id)
    )
    if selected is not None:
        query = query.where(Category.cuisine_id == selected.id)
    if q:
        term = f"%{q.strip().lower()}%"
        query = query.where(func.lower(MenuItem.name).like(term))

    query = query.order_by(Category.position, MenuItem.position).limit(600)
    items = list((await session.execute(query)).scalars())

    # Group by category so the page reads like the sheet does.
    grouped: dict[str, list[MenuItem]] = {}
    for item in items:
        grouped.setdefault(item.category.name, []).append(item)

    # The "Add a dish" form needs somewhere to put it.
    categories: list[Category] = []
    if selected is not None:
        categories = list((await session.execute(
            select(Category)
            .where(Category.cuisine_id == selected.id)
            .order_by(Category.position, Category.name)
        )).scalars())

    return render(request, "admin/menu.html", {
        "current_user": current_user,
        "cuisines": cuisines,
        "selected": selected,
        "categories": categories,
        "grouped": grouped,
        "item_count": len(items),
        "query": q or "",
    })


# --- dishes ---------------------------------------------------------------------
@router.post("/items", name="admin_menu_item_create")
async def create_item(
    request: Request,
    category_id: str = Form(...),
    name: str = Form(...),
    price: str = Form(...),
    cost_price: str = Form(default=""),
    description: str = Form(default=""),
    pack_size: str = Form(default=""),
    serves: str = Form(default=""),
    image_url: str = Form(default=""),
    photo: UploadFile | None = File(default=None),
    is_available: str = Form(default=""),
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    """Add one dish to the menu."""
    back = _back(request, request.query_params.get("cuisine", ""))
    try:
        item = await menu_admin.create_item(
            session,
            category_id=_as_uuid(category_id, "category"),
            name=name, price=price, cost_price=cost_price,
            description=description, pack_size=pack_size, serves=serves,
            image_url=image_url,
            upload_name=photo.filename if photo else None,
            upload_bytes=await _read_upload(photo),
            is_available=is_available == "on",
        )
    except MenuEditError as exc:
        return redirect(back, flash=("error", str(exc)))

    log.info("menu_item_created_by_admin", retailer_id=item.retailer_id,
             by=current_user.email)
    # Re-read with the cuisine joined: the row was only just inserted, so
    # `item.category` is unloaded and touching it would lazy-load mid-request,
    # which SQLAlchemy's async session refuses (MissingGreenlet).
    saved = await _get_item(session, item.id)
    slug = saved.category.cuisine.slug if saved and saved.category else ""
    return redirect(_back(request, slug),
                    flash=("success", f"Added {item.name} to the menu."))


@router.post("/items/{item_id}", name="admin_menu_item_update")
async def update_item(
    request: Request,
    item_id: uuid.UUID,
    price: str = Form(...),
    name: str = Form(default=""),
    cost_price: str | None = Form(default=None),
    description: str | None = Form(default=None),
    pack_size: str | None = Form(default=None),
    serves: str | None = Form(default=None),
    image_url: str = Form(default=""),
    photo: UploadFile | None = File(default=None),
    is_available: str = Form(default=""),
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    """Edit one dish.

    The quick row form sends only price and availability; the full editor
    sends everything. Fields absent from the request are left alone, so the
    quick form cannot silently blank a description.
    """
    item = await _get_item(session, item_id)
    if item is None:
        return redirect(_back(request, ""),
                        flash=("error", "That item no longer exists."))

    slug = item.category.cuisine.slug if item.category else ""
    try:
        await menu_admin.update_item(
            session, item,
            name=name or None, price=price, cost_price=cost_price,
            description=description, pack_size=pack_size, serves=serves,
            image_url=image_url,
            upload_name=photo.filename if photo else None,
            upload_bytes=await _read_upload(photo),
            is_available=is_available == "on",
        )
    except MenuEditError as exc:
        return redirect(_back(request, slug), flash=("error", str(exc)))

    log.info("menu_item_updated_by_admin", retailer_id=item.retailer_id,
             by=current_user.email)
    return redirect(_back(request, slug),
                    flash=("success", f"Updated {item.name}."))


@router.post("/items/{item_id}/delete", name="admin_menu_item_delete")
async def delete_item(
    request: Request,
    item_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    """Remove a dish permanently."""
    item = await _get_item(session, item_id)
    if item is None:
        return redirect(_back(request, ""),
                        flash=("error", "That item no longer exists."))

    slug = item.category.cuisine.slug if item.category else ""
    name = await menu_admin.delete_item(session, item)
    log.info("menu_item_deleted_by_admin", name=name, by=current_user.email)
    return redirect(_back(request, slug),
                    flash=("success", f"Deleted {name}."))


# --- categories -------------------------------------------------------------------
@router.post("/categories", name="admin_category_create")
async def create_category(
    request: Request,
    cuisine_id: str = Form(...),
    name: str = Form(...),
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    try:
        category = await menu_admin.create_category(
            session, _as_uuid(cuisine_id, "cuisine"), name)
    except MenuEditError as exc:
        return redirect(_back(request, request.query_params.get("cuisine", "")),
                        flash=("error", str(exc)))

    cuisine = await session.get(Cuisine, category.cuisine_id)
    log.info("category_created_by_admin", slug=category.slug, by=current_user.email)
    return redirect(_back(request, cuisine.slug if cuisine else ""),
                    flash=("success", f"Added the {category.name} category."))


@router.post("/categories/{category_id}/delete", name="admin_category_delete")
async def delete_category(
    request: Request,
    category_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    """Remove a category and every dish in it."""
    category = (await session.execute(
        select(Category).options(selectinload(Category.cuisine))
        .where(Category.id == category_id)
    )).scalar_one_or_none()
    if category is None:
        return redirect(_back(request, ""),
                        flash=("error", "That category no longer exists."))

    slug = category.cuisine.slug if category.cuisine else ""
    name, removed = await menu_admin.delete_category(session, category)
    log.info("category_deleted_by_admin", name=name, items=removed,
             by=current_user.email)
    return redirect(_back(request, slug), flash=(
        "success",
        f"Deleted {name} and {removed} dish{'es' if removed != 1 else ''}."))


# --- cuisines ----------------------------------------------------------------------
@router.post("/cuisines", name="admin_cuisine_create")
async def create_cuisine(
    request: Request,
    name: str = Form(...),
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    try:
        cuisine = await menu_admin.create_cuisine(session, name)
    except MenuEditError as exc:
        return redirect(_back(request, request.query_params.get("cuisine", "")),
                        flash=("error", str(exc)))

    log.info("cuisine_created_by_admin", slug=cuisine.slug, by=current_user.email)
    return redirect(_back(request, cuisine.slug), flash=(
        "success",
        f"Added {cuisine.name}. Create a category in it to start adding dishes."))


@router.post("/cuisines/{cuisine_id}/delete", name="admin_cuisine_delete")
async def delete_cuisine(
    request: Request,
    cuisine_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    """Remove a cuisine, its categories and all of their dishes."""
    cuisine = await session.get(Cuisine, cuisine_id)
    if cuisine is None:
        return redirect(_back(request, ""),
                        flash=("error", "That cuisine no longer exists."))

    name, removed = await menu_admin.delete_cuisine(session, cuisine)
    log.info("cuisine_deleted_by_admin", name=name, items=removed,
             by=current_user.email)
    return redirect(_back(request, ""), flash=(
        "success",
        f"Deleted {name} and {removed} dish{'es' if removed != 1 else ''}."))


@router.post("/cuisines/{cuisine_id}/toggle", name="admin_cuisine_toggle")
async def toggle_cuisine(
    request: Request,
    cuisine_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    """Show or hide a whole cuisine from the WhatsApp menu."""
    cuisine = await session.get(Cuisine, cuisine_id)
    if cuisine is None:
        return redirect(_back(request, ""),
                        flash=("error", "That cuisine no longer exists."))

    cuisine.is_active = not cuisine.is_active
    state = "visible to customers" if cuisine.is_active else "hidden"
    log.info("cuisine_toggled", slug=cuisine.slug, active=cuisine.is_active,
             by=current_user.email)
    return redirect(_back(request, cuisine.slug),
                    flash=("success", f"{cuisine.name} is now {state}."))


# --- helpers -------------------------------------------------------------------------
def _back(request: Request, cuisine_slug: str) -> str:
    """The menu page, back on the cuisine the person was looking at."""
    url = str(request.url_for("admin_menu"))
    return f"{url}?cuisine={cuisine_slug}" if cuisine_slug else url


def _as_uuid(raw: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        raise MenuEditError(f"Choose a {field}.") from None


async def _get_item(session: AsyncSession, item_id: uuid.UUID) -> MenuItem | None:
    return (await session.execute(
        select(MenuItem)
        .options(selectinload(MenuItem.category).selectinload(Category.cuisine))
        .where(MenuItem.id == item_id)
    )).scalar_one_or_none()


async def _read_upload(upload: UploadFile | None) -> bytes | None:
    """Bytes of an uploaded photo, or None when the field was left empty.

    A browser posts an empty file part for an untouched file input, which
    arrives with no filename - treating that as an upload would replace the
    existing photo with nothing.
    """
    if upload is None or not upload.filename:
        return None
    if upload.content_type and upload.content_type not in menu_admin.ALLOWED_IMAGE_TYPES:
        raise MenuEditError(
            f"'{upload.filename}' is a {upload.content_type} file. "
            f"Please upload a JPEG, PNG, WebP or GIF image."
        )
    return await upload.read()
