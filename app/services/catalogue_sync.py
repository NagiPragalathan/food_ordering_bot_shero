"""Menu dishes -> Zoho Products; kitchens -> Zoho Vendors.

Zoho has modules for both, so the bot links to them rather than copying
names into text: an Order points at the Vendor that cooked it, and each
Order Item points at the Product it is, which is what makes "dishes sold"
reports possible in Zoho.

Same rule as crm_sync: a Zoho failure is logged and reported, never raised
into the ordering flow. Each local row remembers its Zoho id. When that id
is stale (the record was deleted in Zoho, or the bot was pointed at another
org) the record is found again by its code, or created.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import ConfigurationError, IntegrationError
from app.core.logging import get_logger
from app.db.models import Category, MenuItem, Outlet
from app.integrations.zoho import crm
from app.integrations.zoho import fields as f

log = get_logger(__name__)

ZOHO_ERRORS = (IntegrationError, ConfigurationError)


async def ensure_product(item: MenuItem) -> str | None:
    """The dish's Zoho Product id, creating or updating the Product.

    `item.category` and its `cuisine` must be loaded (see dishes_with_labels);
    the async session cannot lazy-load them here.
    """
    fields = crm.product_fields(item, cuisine=item.category.cuisine.name,
                                category=item.category.name)
    try:
        item.zoho_product_id = await _sync(
            f.PRODUCTS, item.zoho_product_id, fields,
            find=lambda: crm.find_product_by_code(item.retailer_id),
            create=crm.create_product, update=crm.update_product)
    except ZOHO_ERRORS as exc:
        log.error("zoho_product_sync_failed", retailer_id=item.retailer_id, error=str(exc))
        return None
    return item.zoho_product_id


async def ensure_vendor(outlet: Outlet) -> str | None:
    """The kitchen's Zoho Vendor id, creating or updating the Vendor."""
    fields = crm.vendor_fields(outlet)
    try:
        outlet.zoho_vendor_id = await _sync(
            f.VENDORS, outlet.zoho_vendor_id, fields,
            find=lambda: crm.find_vendor(outlet.code, outlet.name),
            create=crm.create_vendor, update=crm.update_vendor)
    except ZOHO_ERRORS as exc:
        log.error("zoho_vendor_sync_failed", outlet=outlet.code, error=str(exc))
        return None
    return outlet.zoho_vendor_id


def dishes_with_labels(codes: list[str] | None = None):
    """MenuItem query with category and cuisine loaded, for ensure_product."""
    query = select(MenuItem).options(
        selectinload(MenuItem.category).selectinload(Category.cuisine))
    if codes is not None:
        query = query.where(MenuItem.retailer_id.in_(codes))
    return query


async def push_menu(session: AsyncSession) -> tuple[int, list[str]]:
    """Every dish to Products. Returns (synced, names of dishes that failed)."""
    items = (await session.execute(dishes_with_labels().order_by(MenuItem.name))).scalars()
    synced, failed = 0, []
    for item in items:
        if await ensure_product(item):
            synced += 1
        else:
            failed.append(item.name)
    await session.flush()
    return synced, failed


async def push_outlets(session: AsyncSession) -> tuple[int, list[str]]:
    """Every kitchen to Vendors. Returns (synced, names of kitchens that failed)."""
    outlets = (await session.execute(select(Outlet).order_by(Outlet.name))).scalars()
    synced, failed = 0, []
    for outlet in outlets:
        if await ensure_vendor(outlet):
            synced += 1
        else:
            failed.append(outlet.name)
    await session.flush()
    return synced, failed


async def push_catalogue(session: AsyncSession) -> tuple[int, int, list[str]]:
    """Admin "Sync to Zoho": (dishes synced, kitchens synced, problem sentences)."""
    dishes, failed_dishes = await push_menu(session)
    kitchens, failed_outlets = await push_outlets(session)
    problems = []
    if failed_dishes:
        problems.append(f"Could not sync {len(failed_dishes)} dish(es): "
                        f"{', '.join(failed_dishes[:5])}"
                        f"{' ...' if len(failed_dishes) > 5 else ''} - see the server log.")
    if failed_outlets:
        problems.append(f"Could not sync kitchen(s) {', '.join(failed_outlets)} "
                        "- see the server log.")
    return dishes, kitchens, problems


# --- helpers -----------------------------------------------------------------
async def _sync(module: str, zoho_id: str | None, fields: dict, *,
                find: Callable[[], Awaitable[dict | None]],
                create: Callable[[dict], Awaitable[str | None]],
                update: Callable[[str, dict], Awaitable[None]]) -> str | None:
    """Update the linked record, else the one found by code, else create."""
    if zoho_id:
        try:
            await update(zoho_id, fields)
            return zoho_id
        except IntegrationError as exc:
            if not crm.is_missing_record(exc):
                raise
            log.info("zoho_linked_record_gone", module=module, zoho_id=zoho_id)
    found = await find()
    if found and found.get("id"):
        await update(found["id"], fields)
        return str(found["id"])
    return await create(fields)
