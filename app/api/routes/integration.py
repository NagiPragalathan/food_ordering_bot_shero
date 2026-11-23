"""Read-only integration API: the catalogue and the orders, for a business's
own systems (DST India's CRM reads its menu, cuisines and ordered dishes here).

    GET /api/v1/kitchens                 the kitchens (outlets)
    GET /api/v1/cuisines                 cuisines and their categories
    GET /api/v1/menu                     the dishes, with cuisine, category, price
    GET /api/v1/orders                   orders with their dishes, newest first
    GET /api/v1/orders/{order_number}    one order

Each business at its own path: Shero US at /api/v1/..., DST India at
/dst/api/v1/... (the tenant middleware), each with its own database and its
own key. Authentication: the `X-Api-Key` header, that business's
INTEGRATION_API_KEY, made on its admin Settings page (Integration API card).
Without a key set the API answers 503 and gives nothing out.

Nothing here changes anything. Orders use the same order and customer shapes
the DST CRM already receives in its pushed events (services/dst_crm.py), plus
what a reader needs besides: timestamps, payment status, the kitchen's code,
each dish's cuisine and category, and the courier.
"""

from __future__ import annotations

import hmac
from datetime import date, datetime, time, timezone
from decimal import Decimal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.db.models import Category, Cuisine, Customer, MenuItem, Order, Outlet
from app.db.session import get_session
from app.services import dst_crm

router = APIRouter(prefix="/api/v1", tags=["integration"])

MAX_LIMIT = 500
PAYMENT_STATUSES = ("not_started", "link_sent", "paid", "failed", "expired", "refunded")


def require_api_key(response: Response, x_api_key: str | None = Header(default=None)) -> None:
    """This business's integration key, compared in constant time."""
    response.headers["Cache-Control"] = "no-store"
    key = (settings.integration_api_key or "").strip()
    if not key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "The integration API is off: generate a key on the admin Settings page.")
    if not x_api_key or not hmac.compare_digest(x_api_key.strip(), key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or wrong X-Api-Key.")


# --- helpers -------------------------------------------------------------------------
def _money(value) -> str | None:
    return None if value is None else f"{Decimal(str(value)):.2f}"


def _when(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _absolute(url: str | None) -> str | None:
    """A dish photo's full link: kept photos are /media/... at the site's root."""
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return url
    parts = urlsplit(settings.public_base_url)
    return f"{parts.scheme}://{parts.netloc}/{url.lstrip('/')}"


def _page(items: list, total: int, limit: int, offset: int) -> dict:
    after = offset + len(items)
    return {"data": items, "total": total, "limit": limit, "offset": offset,
            "next_offset": after if after < total else None}


def _moment(raw: str | None, name: str, *, end: bool = False) -> datetime | None:
    """A date (2026-11-23: the start, or with end=True the end, of that UTC day)
    or an ISO date-time (2026-11-23T10:30:00Z)."""
    if not raw:
        return None
    text = raw.strip().replace("Z", "+00:00")
    try:
        if len(text) == 10:
            day = date.fromisoformat(text)
            return datetime.combine(day, time.max if end else time.min, tzinfo=timezone.utc)
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"{name}: use a date (2026-11-23) or an ISO date-time "
                            "(2026-11-23T10:30:00Z).") from None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# --- kitchens ----------------------------------------------------------------------------
@router.get("/kitchens", dependencies=[Depends(require_api_key)])
async def kitchens(session: AsyncSession = Depends(get_session)) -> dict:
    """Every kitchen (outlet): its code, address and the cuisines it cooks."""
    rows = (await session.execute(select(Outlet).order_by(Outlet.name))).scalars().all()
    data = [{
        "id": str(o.id), "code": o.code, "name": o.name, "is_active": o.is_active,
        "is_primary": o.is_primary,
        "address": {"line1": o.address_line1, "line2": o.address_line2, "city": o.city,
                    "state": o.state, "pincode": o.postal_code, "country": o.country},
        "latitude": o.latitude, "longitude": o.longitude, "phone": o.phone,
        "cuisines": list(o.cuisines or []), "timezone": o.timezone,
        "updated_at": _when(o.updated_at),
    } for o in rows]
    return {"data": data, "total": len(data)}


# --- cuisines -----------------------------------------------------------------------------
@router.get("/cuisines", dependencies=[Depends(require_api_key)])
async def cuisines(active_only: bool = False,
                   session: AsyncSession = Depends(get_session)) -> dict:
    """Cuisines with their categories (the menu's tabs and sections), in menu order."""
    query = select(Cuisine).options(selectinload(Cuisine.categories)).order_by(
        Cuisine.position, Cuisine.name)
    if active_only:
        query = query.where(Cuisine.is_active.is_(True))
    counts = dict((await session.execute(
        select(MenuItem.category_id, func.count()).group_by(MenuItem.category_id))).all())
    data = []
    for cuisine in (await session.execute(query)).scalars().all():
        categories = sorted(cuisine.categories, key=lambda c: (c.position, c.name))
        data.append({
            "id": str(cuisine.id), "name": cuisine.name, "slug": cuisine.slug,
            "description": cuisine.description, "position": cuisine.position,
            "is_active": cuisine.is_active, "updated_at": _when(cuisine.updated_at),
            "categories": [{
                "id": str(cat.id), "name": cat.name, "slug": cat.slug,
                "position": cat.position, "is_active": cat.is_active,
                "item_count": counts.get(cat.id, 0),
            } for cat in categories if cat.is_active or not active_only],
        })
    return {"data": data, "total": len(data)}


# --- menu ---------------------------------------------------------------------------------
def _dish(item: MenuItem) -> dict:
    category = item.category
    cuisine = category.cuisine if category is not None else None
    return {
        "id": str(item.id), "sku": item.retailer_id, "name": item.name,
        "description": item.description,
        "cuisine": {"id": str(cuisine.id), "name": cuisine.name, "slug": cuisine.slug}
        if cuisine else None,
        "category": {"id": str(category.id), "name": category.name, "slug": category.slug}
        if category else None,
        "price": _money(item.price), "currency": (item.currency or settings.currency).upper(),
        "pack_size": item.pack_size, "serves": item.serves,
        "image_url": _absolute(item.image_url),
        "is_available": item.is_available, "position": item.position,
        "updated_at": _when(item.updated_at),
    }


@router.get("/menu", dependencies=[Depends(require_api_key)])
async def menu(cuisine: str | None = None, category: str | None = None,
               available: bool | None = None, updated_since: str | None = None,
               q: str | None = None,
               limit: int = Query(200, ge=1, le=MAX_LIMIT), offset: int = Query(0, ge=0),
               session: AsyncSession = Depends(get_session)) -> dict:
    """The dishes, each with its cuisine and category. Filter by cuisine or
    category slug, availability, a name search, or what changed since a time."""
    where = []
    if cuisine:
        where.append(Cuisine.slug == cuisine.strip().lower())
    if category:
        where.append(Category.slug == category.strip().lower())
    if available is not None:
        where.append(MenuItem.is_available.is_(available))
    since = _moment(updated_since, "updated_since")
    if since is not None:
        where.append(MenuItem.updated_at >= since)
    if q and q.strip():
        where.append(MenuItem.name.ilike(f"%{q.strip()}%"))
    base = (select(MenuItem).join(Category, MenuItem.category_id == Category.id)
            .join(Cuisine, Category.cuisine_id == Cuisine.id).where(*where))
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = (await session.execute(
        base.options(selectinload(MenuItem.category).selectinload(Category.cuisine))
        .order_by(Cuisine.position, Category.position, MenuItem.position, MenuItem.name)
        .limit(limit).offset(offset))).scalars().all()
    return _page([_dish(i) for i in rows], total, limit, offset)


# --- orders -------------------------------------------------------------------------------
async def _dish_index(session: AsyncSession, skus: set[str]) -> dict[str, MenuItem]:
    if not skus:
        return {}
    rows = (await session.execute(
        select(MenuItem).where(MenuItem.retailer_id.in_(skus))
        .options(selectinload(MenuItem.category).selectinload(Category.cuisine)))).scalars()
    return {i.retailer_id: i for i in rows}


def _order(order: Order, customer: Customer | None, outlet: Outlet | None,
           dishes: dict[str, MenuItem]) -> dict:
    body = dst_crm.order_fields(order, kitchen=outlet.name if outlet else None)
    for line in body["items"]:
        dish = dishes.get(line.get("sku") or "")
        category = dish.category if dish else None
        line["cuisine"] = category.cuisine.name if category and category.cuisine else None
        line["category"] = category.name if category else None
    courier = order.courier or {}
    body["delivery_date"] = _when(order.slot_starts_at)      # UTC, like every time here
    return {
        **body,
        "currency": (order.currency or settings.currency).upper(),
        "payment_status": str(order.payment_status),
        "test_payment": body.get("payment_reference") == dst_crm.TEST_PAYMENT,
        "kitchen_code": outlet.code if outlet else None,
        "created_at": _when(order.created_at), "updated_at": _when(order.updated_at),
        "paid_at": _when(order.paid_at),
        "timeline": {"sent_to_kitchen_at": _when(order.sent_to_kitchen_at),
                     "ready_at": _when(order.ready_at),
                     "out_for_delivery_at": _when(order.out_for_delivery_at),
                     "delivered_at": _when(order.delivered_at),
                     "cancelled_at": _when(order.cancelled_at)},
        "courier": {k: courier.get(k) for k in ("provider", "courier_name", "awb", "status",
                                                "tracking_url", "fee", "eta")
                    if courier.get(k) not in (None, "")} or None,
        "customer": dst_crm.customer_fields(customer) if customer else None,
    }


async def _orders_out(session: AsyncSession, rows: list[Order]) -> list[dict]:
    customers = {c.id: c for c in (await session.execute(select(Customer).where(
        Customer.id.in_({o.customer_id for o in rows})))).scalars()} if rows else {}
    outlets = {o.id: o for o in (await session.execute(select(Outlet).where(
        Outlet.id.in_({o.outlet_id for o in rows if o.outlet_id})))).scalars()} if rows else {}
    skus = {str(line.get("retailer_id")) for o in rows for line in (o.items or [])
            if line.get("retailer_id")}
    dishes = await _dish_index(session, skus)
    return [_order(o, customers.get(o.customer_id), outlets.get(o.outlet_id), dishes)
            for o in rows]


@router.get("/orders", dependencies=[Depends(require_api_key)])
async def orders(payment_status: str = "paid", stage: str | None = None,
                 kitchen: str | None = None, phone: str | None = None,
                 created_from: str | None = Query(None, alias="from"),
                 created_to: str | None = Query(None, alias="to"),
                 updated_since: str | None = None,
                 limit: int = Query(100, ge=1, le=MAX_LIMIT), offset: int = Query(0, ge=0),
                 session: AsyncSession = Depends(get_session)) -> dict:
    """Orders with their dishes, newest first. Paid ones unless payment_status
    says otherwise ("all" for every order, carts not paid included)."""
    where = []
    wanted = (payment_status or "paid").strip().lower()
    if wanted != "all":
        if wanted not in PAYMENT_STATUSES:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "payment_status: one of all, " + ", ".join(PAYMENT_STATUSES))
        where.append(Order.payment_status == wanted)
    if stage:
        where.append(func.lower(Order.stage) == stage.strip().lower())
    if kitchen:
        outlet = (await session.execute(select(Outlet).where(
            func.lower(Outlet.code) == kitchen.strip().lower()))).scalar_one_or_none()
        if outlet is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No kitchen with that code.")
        where.append(Order.outlet_id == outlet.id)
    if phone:
        digits = "".join(ch for ch in phone if ch.isdigit())
        if digits:
            ids = select(Customer.id).where(Customer.whatsapp_number.like(f"%{digits[-10:]}"))
            where.append(or_(Order.customer_id.in_(ids),
                             Order.contact_number.like(f"%{digits[-10:]}")))
    start, end = _moment(created_from, "from"), _moment(created_to, "to", end=True)
    if start is not None:
        where.append(Order.created_at >= start)
    if end is not None:
        where.append(Order.created_at <= end)
    since = _moment(updated_since, "updated_since")
    if since is not None:
        where.append(Order.updated_at >= since)
    total = await session.scalar(select(func.count()).select_from(Order).where(*where)) or 0
    rows = (await session.execute(select(Order).where(*where)
                                  .order_by(Order.created_at.desc())
                                  .limit(limit).offset(offset))).scalars().all()
    return _page(await _orders_out(session, list(rows)), total, limit, offset)


@router.get("/orders/{order_number}", dependencies=[Depends(require_api_key)])
async def order(order_number: str, session: AsyncSession = Depends(get_session)) -> dict:
    """One order, paid or not, by its number (e.g. DST-261119-S9G1C)."""
    found = (await session.execute(select(Order).where(
        func.upper(Order.order_number) == order_number.strip().upper()))).scalar_one_or_none()
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No order with that number.")
    return {"data": (await _orders_out(session, [found]))[0]}


def base_url() -> str:
    """This business's API address (its PUBLIC_BASE_URL already carries /dst)."""
    return settings.public_base_url.rstrip("/") + "/api/v1"
