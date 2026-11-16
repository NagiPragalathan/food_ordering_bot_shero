"""The menu from the Shero ERP's item exports (Excel).

The ERP exports one workbook per cuisine ("Shero Home Food - Andhra.xlsx"):
a few title rows ("Shero Home - Item Details:", "Export Date:..."), then one
row per dish under the header

    Item_code  Name  Category  Brand  Description  image  Type  ...
    Food_type  Hsn  Prep_time  [Pp_price]  Mrp  Package_Charge  ...

Each workbook becomes one cuisine (a tab on the ordering page), named after
the file or typed on the Import page. Its Category column gives the
sections, Mrp is what the customer pays, and Pp_price - when the export
has it - is stored as the cost (never shown to a customer). The ERP's item
code is the dish's id here, so uploading a newer export updates the same
dishes instead of adding copies.

An export sometimes lists every brand (a full catalogue named after one
cuisine). Then only the rows of that cuisine's own brand are taken, and the
page says how many others were left out. A file of a single brand is taken
whole, whatever the brand is called in the ERP.

With "replace the whole menu", every dish that is not in the files is
deleted, and categories and cuisines left empty go with it. Orders keep
their own copy of what was bought, so past orders are unaffected.

Photos stay at their ERP links until `store_remote_photos` (a scheduled job,
a batch at a time) copies each to this bot's own store, so a customer's page
never depends on the ERP's servers.
"""

from __future__ import annotations

import io
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import PurePath

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Category, Cuisine, MenuItem
from app.services import media
from app.services.menu_import import ImportResult, slugify

log = get_logger(__name__)

MAX_FILE_BYTES = 10 * 1024 * 1024
HEADER_SCAN_ROWS = 20
PHOTO_BATCH = 40


class ErpImportError(ValueError):
    """A file this importer cannot use; the message says why, for the page."""


@dataclass
class ErpDish:
    code: str
    name: str
    category: str
    brand: str
    description: str
    image: str
    price: Decimal | None
    cost: Decimal | None
    food_type: str
    row: int


@dataclass
class ErpFile:
    filename: str
    cuisine: str
    dishes: list[ErpDish] = field(default_factory=list)


def _norm(text) -> str:
    return " ".join(str(text or "").split())


def _key(text) -> str:
    return _norm(text).lower()


def _money(value) -> Decimal | None:
    text = _norm(value).replace(",", "").replace("₹", "").replace("Rs.", "").strip()
    if not text:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    return amount.quantize(Decimal("0.01")) if amount >= 0 else None


def cuisine_from_filename(filename: str) -> str:
    """"Shero Home Food - North Indian.xlsx" -> "North Indian"."""
    stem = PurePath(filename or "").stem
    name = stem.rsplit(" - ", 1)[-1] if " - " in stem else stem
    return _norm(re.sub(r"\s*\(\d+\)$", "", name))      # "Andhra (1)" from a re-download


def read_export(data: bytes, filename: str = "") -> list[ErpDish]:
    """Every dish row of one ERP export. Raises ErpImportError on anything
    that is not one."""
    from openpyxl import load_workbook

    label = filename or "That file"
    if len(data) > MAX_FILE_BYTES:
        raise ErpImportError(f"{label} is larger than {MAX_FILE_BYTES // 1024 // 1024} MB.")
    try:
        sheet = load_workbook(io.BytesIO(data), read_only=True, data_only=True).active
    except Exception as exc:  # noqa: BLE001 - zip, xml and format errors alike
        raise ErpImportError(f"{label} could not be opened as an Excel (.xlsx) file.") from exc

    rows = sheet.iter_rows(values_only=True)
    columns: dict[str, int] | None = None
    for number, row in enumerate(rows, start=1):
        cells = [_key(c) for c in row]
        if "item_code" in cells and "name" in cells:
            columns = {name: index for index, name in enumerate(cells) if name}
            header_row = number
            break
        if number >= HEADER_SCAN_ROWS:
            break
    if columns is None or "mrp" not in columns:
        raise ErpImportError(
            f"{label} is not an ERP item export: no header row with Item_code, Name "
            "and Mrp in its first rows.")

    def cell(row, name):
        index = columns.get(name)
        return row[index] if index is not None and index < len(row) else None

    dishes: list[ErpDish] = []
    for offset, row in enumerate(rows, start=header_row + 1):
        code, name = _norm(cell(row, "item_code")), _norm(cell(row, "name"))
        if not code or not name:
            continue
        dishes.append(ErpDish(
            code=code, name=name[:200],
            category=_norm(cell(row, "category")) or "Other",
            brand=_norm(cell(row, "brand")),
            description=_norm(cell(row, "description")),
            image=_norm(cell(row, "image")),
            price=_money(cell(row, "mrp")),
            cost=_money(cell(row, "pp_price")),
            food_type=_norm(cell(row, "food_type")),
            row=offset))
    if not dishes:
        raise ErpImportError(f"{label} has the ERP header but no dishes under it.")
    return dishes


def own_dishes(file: ErpFile, result: ImportResult) -> list[ErpDish]:
    """The rows of the file's own cuisine (see the module doc)."""
    brands = Counter(_key(d.brand) for d in file.dishes)
    if len(brands) <= 1:
        only = next(iter(brands), "")
        if only and only != _key(file.cuisine):
            result.warnings.append(
                f"{file.filename}: every dish is marked brand \"{file.dishes[0].brand}\" in "
                f"the ERP; imported as {file.cuisine}.")
        return file.dishes
    kept = [d for d in file.dishes if _key(d.brand) == _key(file.cuisine)]
    if not kept:
        names = ", ".join(sorted({d.brand for d in file.dishes if d.brand}))
        raise ErpImportError(
            f"{file.filename} lists several brands ({names}) and none is called "
            f"{file.cuisine}. Type the brand to take in the cuisine box.")
    result.warnings.append(
        f"{file.filename}: took its {len(kept)} {file.cuisine} dishes; the other "
        f"{len(file.dishes) - len(kept)} rows are other brands' and were left out.")
    return kept


def _retailer_id(code: str) -> str:
    return code.strip().lower()


def _same_photo(current: str | None, source: str) -> bool:
    """Is `current` already our stored copy of the ERP photo `source`?"""
    if not current or not source:
        return False
    if current == source:
        return True
    digest = media.local_path_for(source, "image/jpeg").stem
    return media.is_ours(current) and digest in current


async def import_exports(session: AsyncSession, files: list[tuple[str, bytes, str]], *,
                         replace: bool = False) -> ImportResult:
    """Import ERP exports: [(filename, contents, cuisine name or "")]."""
    result = ImportResult()
    parsed: list[ErpFile] = []
    for filename, data, cuisine in files:
        parsed.append(ErpFile(filename=filename or "file",
                              cuisine=_norm(cuisine) or cuisine_from_filename(filename),
                              dishes=read_export(data, filename)))
    # A cost price found in any file counts for the same item code everywhere
    # (only the full catalogue export carries Pp_price).
    costs = {d.code: d.cost for f in parsed for d in f.dishes if d.cost is not None}

    taken: dict[str, str] = {}              # item code -> the cuisine that took it
    plan: list[tuple[str, list[ErpDish]]] = []
    for file in parsed:
        if not file.cuisine:
            raise ErpImportError(f"{file.filename}: type the cuisine name for it.")
        dishes = []
        for dish in own_dishes(file, result):
            if dish.code in taken:
                result.warnings.append(f"{dish.code} {dish.name} is in both {taken[dish.code]} "
                                       f"and {file.cuisine}; kept in {taken[dish.code]}.")
                continue
            taken[dish.code] = file.cuisine
            dishes.append(dish)
        plan.append((file.cuisine, dishes))

    existing_cuisines = {c.slug: c for c in (await session.execute(select(Cuisine))).scalars()}
    existing_items = {i.retailer_id: i for i in (await session.execute(select(MenuItem))).scalars()}
    next_cuisine = int(await session.scalar(select(func.max(Cuisine.position))) or 0) + 1
    unpriced = 0

    for cuisine_name, dishes in plan:
        slug = slugify(cuisine_name, max_length=80)
        cuisine = existing_cuisines.get(slug)
        if cuisine is None:
            cuisine = Cuisine(name=cuisine_name[:80], slug=slug, position=next_cuisine,
                              is_active=True)
            next_cuisine += 1
            session.add(cuisine)
            await session.flush()
            existing_cuisines[slug] = cuisine
        result.cuisines.append(cuisine.name)
        categories = {c.slug: c for c in (await session.execute(
            select(Category).where(Category.cuisine_id == cuisine.id))).scalars()}

        for position, dish in enumerate(dishes, start=1):
            category_slug = slugify(dish.category, max_length=120)
            category = categories.get(category_slug)
            if category is None:
                category = Category(cuisine_id=cuisine.id, name=dish.category[:120],
                                    slug=category_slug, position=len(categories) + 1,
                                    is_active=True)
                session.add(category)
                await session.flush()
                categories[category_slug] = category
                result.categories_created += 1

            price = dish.price or Decimal("0.00")
            if price <= 0:
                unpriced += 1
            item = existing_items.get(_retailer_id(dish.code))
            if item is None:
                item = MenuItem(retailer_id=_retailer_id(dish.code), currency=settings.currency.upper(),
                                is_available=price > 0)
                session.add(item)
                existing_items[item.retailer_id] = item
                result.items_created += 1
            else:
                result.items_updated += 1
                if price <= 0:
                    item.is_available = False
            item.category_id = category.id
            item.name = dish.name
            item.description = dish.description or None
            item.price = price
            item.cost_price = dish.cost if dish.cost is not None else costs.get(dish.code)
            item.currency = settings.currency.upper()
            item.position = position
            # A new ERP photo replaces the old (copied to our store later); a
            # dish the ERP has no photo for keeps one the team uploaded here.
            if dish.image and not _same_photo(item.image_url, dish.image):
                item.image_url = dish.image
    await session.flush()

    if replace:
        keep = {_retailer_id(code) for code in taken}
        gone = [i for rid, i in existing_items.items() if rid not in keep]
        for item in gone:
            await session.delete(item)
        await session.flush()
        result.items_removed = len(gone)
        # Sections and cuisines left with nothing in them.
        await session.execute(delete(Category).where(
            ~Category.id.in_(select(MenuItem.category_id).distinct())))
        await session.execute(delete(Cuisine).where(
            ~Cuisine.id.in_(select(Category.cuisine_id).distinct())))
        await session.flush()

    if unpriced:
        result.warnings.append(f"{unpriced} dish(es) have no Mrp in the ERP: they are on "
                               "the menu switched off until they have a price.")
    pending = await photos_waiting(session)
    if pending:
        result.warnings.append(f"{pending} photo(s) are being copied from the ERP to our "
                               f"server, {PHOTO_BATCH} every few minutes; until then they "
                               "load from the ERP's link.")
    log.info("erp_menu_imported", files=len(files), replace=replace,
             summary=result.summary(), removed=result.items_removed)
    return result


# --- photos ----------------------------------------------------------------------
# Where the ERP keeps its item photos. Only these links are copied: a photo
# linked from anywhere else (none today) is left exactly as it is.
ERP_PHOTOS = "https://sherohomeerp"


def _remote():
    return MenuItem.image_url.like(ERP_PHOTOS + "%")


async def photos_waiting(session: AsyncSession) -> int:
    """Dishes whose photo is still a link to the ERP's server."""
    rows = (await session.execute(select(MenuItem.image_url).where(_remote()))).scalars()
    return sum(1 for url in rows if url and not media.is_ours(url))


async def store_remote_photos(session: AsyncSession, limit: int = PHOTO_BATCH) -> int:
    """Copy up to `limit` linked photos to our own store; returns how many.

    A photo that cannot be fetched keeps its link (the page still shows it)
    and is tried again on a later run."""
    rows = list((await session.execute(
        select(MenuItem).where(_remote()).order_by(MenuItem.position).limit(limit * 3)
    )).scalars())
    rows = [r for r in rows if r.image_url and not media.is_ours(r.image_url)][:limit]
    if not rows:
        return 0
    stored = await media.fetch_many([r.image_url for r in rows])
    copied = 0
    for item in rows:
        local = stored.get(item.image_url)
        if local:
            item.image_url = local
            copied += 1
    await session.flush()
    if copied:
        log.info("menu_photos_stored", count=copied, left=max(0, len(rows) - copied))
    return copied
