"""Menu browser and per-item editing."""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.admin.deps import redirect, render, require_admin
from app.core.logging import get_logger
from app.db.models import AdminUser, Category, Cuisine, MenuItem
from app.db.session import get_session

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

    return render(request, "admin/menu.html", {
        "current_user": current_user,
        "cuisines": cuisines,
        "selected": selected,
        "grouped": grouped,
        "item_count": len(items),
        "query": q or "",
    })


@router.post("/items/{item_id}", name="admin_menu_item_update")
async def update_item(
    request: Request,
    item_id: uuid.UUID,
    price: str = Form(...),
    is_available: str = Form(default=""),
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    """Edit one item's price and availability."""
    item = await session.get(MenuItem, item_id)
    if item is None:
        return redirect(str(request.url_for("admin_menu")),
                        flash=("error", "That item no longer exists."))

    try:
        new_price = Decimal(price.replace("$", "").strip()).quantize(Decimal("0.01"))
    except (InvalidOperation, AttributeError):
        return redirect(str(request.url_for("admin_menu")),
                        flash=("error", f"'{price}' is not a valid price."))

    if new_price < 0:
        return redirect(str(request.url_for("admin_menu")),
                        flash=("error", "Price cannot be negative."))

    item.price = new_price
    item.is_available = is_available == "on"
    log.info("menu_item_updated", retailer_id=item.retailer_id,
             price=str(new_price), available=item.is_available,
             by=current_user.email)

    cuisine_slug = item.category.cuisine.slug if item.category else ""
    url = str(request.url_for("admin_menu"))
    return redirect(f"{url}?cuisine={cuisine_slug}",
                    flash=("success", f"Updated {item.name}."))


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
        return redirect(str(request.url_for("admin_menu")),
                        flash=("error", "That cuisine no longer exists."))

    cuisine.is_active = not cuisine.is_active
    state = "visible to customers" if cuisine.is_active else "hidden"
    log.info("cuisine_toggled", slug=cuisine.slug, active=cuisine.is_active,
             by=current_user.email)
    return redirect(f"{request.url_for('admin_menu')}?cuisine={cuisine.slug}",
                    flash=("success", f"{cuisine.name} is now {state}."))
