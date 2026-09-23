"""Import the menu from the client's Google Sheet (or an uploaded CSV).

Sheet layout, one tab per cuisine:

    row 1  (blank)
    row 2  |    | Chettinad Cuisine - USA Menu |          |       |     |     |
    row 3  |    | 1 | Sambar        | Description | IMAGE | PPP | MRP |   <- category
    row 4  |    | 1 | Drumstick ... | 16 oz pack. | ...   |10.78|16.53|   <- item

A row is a **category header** when its description cell literally reads
"Description"; anything after it with a name and an MRP is an **item** in that
category.

Two prices per row. MRP is what the customer pays; PPP is the internal cost,
stored for margin reporting and never shown to a customer.

Imports are idempotent. Rows are matched on a derived `retailer_id`, so
re-importing an edited sheet updates prices in place. Items that disappear
from the sheet are **deactivated, not deleted** - an order history that
references them must stay readable.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import IntegrationError
from app.core.logging import get_logger
from app.services import media, sheet_images
from app.db.models import Category, Cuisine, MenuItem

log = get_logger(__name__)

# The XLSX export carries every photo, so it is large. These bound how
# long an import may wait and how much it will hold in memory.
WORKBOOK_TIMEOUT_SECONDS = 300.0
MAX_WORKBOOK_BYTES = 600 * 1024 * 1024

HEADER_MARKER = "description"
RETAILER_ID_MAX = 100
# Slugify without truncating, when the caller handles length itself.
NO_LIMIT = 10_000
SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")
GID_RE = re.compile(r'items\.push\(\{name: "((?:[^"\\]|\\.)*)".*?gid: "(\d+)"')
# "16 oz pack", "26 oz.", "32 oz"
PACK_RE = re.compile(r"(\d+(?:\.\d+)?\s*oz(?:\s*pack)?)", re.IGNORECASE)
# "Serves 3 - 4", "Serves 2-3"
SERVES_RE = re.compile(r"(Serves\s*\d+(?:\s*[-–]\s*\d+)?)", re.IGNORECASE)
# "Chettinad Cuisine - USA Menu", "Andhra Cuisine -  - USA Menu"
TITLE_RE = re.compile(r"^(.*?)\s*cuisine\b", re.IGNORECASE)


@dataclass
class ParsedItem:
    name: str
    description: str
    image_url: str
    price: Decimal          # MRP
    cost_price: Decimal | None  # PPP
    category: str
    position: int
    # 1-based row in the source tab. The photos live in the XLSX export
    # anchored to a cell, so the row is how a picture finds its dish.
    source_row: int = 0


@dataclass
class ParsedTab:
    cuisine: str
    items: list[ParsedItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def categories(self) -> list[str]:
        seen: dict[str, None] = {}
        for item in self.items:
            seen.setdefault(item.category, None)
        return list(seen)


@dataclass
class ImportResult:
    cuisines: list[str] = field(default_factory=list)
    categories_created: int = 0
    items_created: int = 0
    items_updated: int = 0
    items_deactivated: int = 0
    images_stored: int = 0
    images_failed: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def items_total(self) -> int:
        return self.items_created + self.items_updated

    def summary(self) -> str:
        parts = [
            f"{len(self.cuisines)} cuisine(s)",
            f"{self.categories_created} new categor(ies)",
            f"{self.items_created} item(s) added",
            f"{self.items_updated} updated",
        ]
        if self.items_deactivated:
            parts.append(f"{self.items_deactivated} deactivated")
        if self.images_stored:
            parts.append(f"{self.images_stored} image(s) stored")
        if self.images_failed:
            parts.append(f"{self.images_failed} image(s) could not be fetched")
        return ", ".join(parts)


# --- parsing -----------------------------------------------------------------
def parse_csv(text: str, *, fallback_cuisine: str = "") -> ParsedTab:
    """Turn one tab's CSV into a cuisine with its items."""
    rows = list(csv.reader(io.StringIO(text)))
    tab = ParsedTab(cuisine=fallback_cuisine)

    current_category = ""
    position = 0

    for line_number, raw in enumerate(rows, start=1):
        cells = [c.strip() for c in (raw + [""] * 8)[:8]]
        # The name sits in column 2 on most tabs and column 3 on others.
        name = cells[2] or cells[1]
        description, image, ppp, mrp = cells[3], cells[4], cells[5], cells[6]

        if not any(cells):
            continue

        # Title row: "Chettinad Cuisine - USA Menu"
        if not tab.cuisine or tab.cuisine == fallback_cuisine:
            detected = _cuisine_from_title(name)
            if detected:
                tab.cuisine = detected
                continue

        # Category header row.
        if description.lower() == HEADER_MARKER and name:
            current_category = name
            continue

        if not name or not mrp:
            continue

        if not current_category:
            tab.warnings.append(
                f"row {line_number}: '{name}' appears before any category header, skipped"
            )
            continue

        price = _to_decimal(mrp)
        if price is None:
            tab.warnings.append(f"row {line_number}: '{name}' has an unreadable price '{mrp}'")
            continue

        position += 1
        tab.items.append(ParsedItem(
            name=name,
            description=_clean_description(description),
            # Kept raw: a Drive link or an =IMAGE() formula is still a
            # source. `media.normalise_source` works out what to fetch.
            image_url=media.normalise_source(image),
            price=price,
            cost_price=_to_decimal(ppp),
            category=current_category,
            position=position,
            source_row=line_number,
        ))

    if not tab.cuisine:
        tab.warnings.append("could not detect a cuisine name from the sheet title")
    return tab


def _cuisine_from_title(text: str) -> str:
    """'Chettinad Cuisine - USA Menu' -> 'Chettinad'."""
    match = TITLE_RE.match(text.strip())
    if not match:
        return ""
    name = match.group(1).strip(" -–")
    return name if 1 < len(name) <= 40 else ""


def _clean_description(text: str) -> str:
    """Tidy the run-together description cells from the sheet."""
    cleaned = " ".join(text.split())
    # The sheet often has "1 meal.A medium-thin" with no space after the stop.
    return re.sub(r"\.(?=[A-Z])", ". ", cleaned)


def extract_pack_details(description: str) -> tuple[str | None, str | None]:
    pack = PACK_RE.search(description or "")
    serves = SERVES_RE.search(description or "")
    return (
        pack.group(1).strip() if pack else None,
        serves.group(1).strip() if serves else None,
    )


def _to_decimal(value: str) -> Decimal | None:
    text = (value or "").replace("$", "").replace(",", "").strip()
    if not text:
        return None
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def slugify(text: str, *, max_length: int = 60) -> str:
    """Lowercase, hyphenated, ASCII-only."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return cleaned[:max_length].strip("-") or "item"


def build_retailer_id(cuisine: str, category: str, name: str) -> str:
    """Stable id for a menu row.

    Derived from cuisine + category + name so a re-import matches existing
    rows. When truncation is needed, a short hash of the full string keeps it
    unique.
    """
    # Slugify without truncating first: the hash below must be taken over the
    # *whole* identity, or two long names that differ only near the end would
    # collide on a column that is UNIQUE.
    full = "-".join((
        slugify(cuisine, max_length=NO_LIMIT),
        slugify(category, max_length=NO_LIMIT),
        slugify(name, max_length=NO_LIMIT),
    ))
    if len(full) <= RETAILER_ID_MAX:
        return full

    digest = hashlib.sha1(full.encode()).hexdigest()[:8]
    return f"{full[: RETAILER_ID_MAX - 9].strip('-')}-{digest}"


# --- fetching ----------------------------------------------------------------
def sheet_id_from_url(url: str) -> str:
    match = SHEET_ID_RE.search(url or "")
    if not match:
        raise ValueError(
            "That does not look like a Google Sheets URL. Expected something "
            "like https://docs.google.com/spreadsheets/d/<id>/edit"
        )
    return match.group(1)


async def discover_tabs(sheet_id: str) -> list[tuple[str, str]]:
    """Every tab as (name, gid). Requires the sheet to be link-viewable."""
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/htmlview"
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        response = await client.get(url)

    if response.status_code != 200:
        raise IntegrationError(
            "google_sheets",
            f"could not open the sheet (HTTP {response.status_code}). Set its "
            'sharing to "Anyone with the link can view".',
            status_code=response.status_code,
        )

    tabs = [
        (name.encode().decode("unicode_escape"), gid)
        for name, gid in GID_RE.findall(response.text)
    ]
    if not tabs:
        raise IntegrationError(
            "google_sheets",
            "no tabs found in that sheet - check it is shared for viewing",
        )
    return tabs


async def fetch_tab_csv(sheet_id: str, gid: str) -> str:
    url = (f"https://docs.google.com/spreadsheets/d/{sheet_id}"
           f"/export?format=csv&gid={gid}")
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        response = await client.get(url)

    if response.status_code != 200:
        raise IntegrationError(
            "google_sheets",
            f"could not export tab {gid} (HTTP {response.status_code})",
            status_code=response.status_code,
        )
    return response.text


async def fetch_workbook(sheet_id: str) -> bytes:
    """The whole sheet as XLSX, which is the only export carrying the photos.

    Much heavier than a CSV - a few hundred megabytes for a menu with a
    picture per dish - so it gets a long timeout and a size ceiling rather
    than the CSV client's settings.
    """
    url = (f"https://docs.google.com/spreadsheets/d/{sheet_id}"
           f"/export?format=xlsx")
    async with httpx.AsyncClient(timeout=WORKBOOK_TIMEOUT_SECONDS,
                                 follow_redirects=True) as client:
        response = await client.get(url)

    if response.status_code != 200:
        raise IntegrationError(
            "google_sheets",
            f"could not export the workbook (HTTP {response.status_code})",
            status_code=response.status_code,
        )
    if len(response.content) > MAX_WORKBOOK_BYTES:
        raise IntegrationError(
            "google_sheets",
            f"the workbook is {len(response.content) // 1_000_000} MB, over the "
            f"{MAX_WORKBOOK_BYTES // 1_000_000} MB ceiling",
        )
    return response.content


# --- importing ---------------------------------------------------------------
async def import_from_sheet(session: AsyncSession, sheet_url: str, *,
                            deactivate_missing: bool = True,
                            with_photos: bool = True) -> ImportResult:
    """Import every tab of a Google Sheet."""
    sheet_id = sheet_id_from_url(sheet_url)
    tabs = await discover_tabs(sheet_id)
    log.info("sheet_tabs_found", count=len(tabs),
             names=[name for name, _ in tabs])

    parsed: list[ParsedTab] = []
    for name, gid in tabs:
        csv_text = await fetch_tab_csv(sheet_id, gid)
        tab = parse_csv(csv_text, fallback_cuisine=_tab_name_to_cuisine(name))
        if not tab.items:
            tab.warnings.append(f"tab '{name}' had no importable rows")
        parsed.append(tab)

    if with_photos:
        await _attach_sheet_photos(sheet_id, parsed)

    return await apply(session, parsed, deactivate_missing=deactivate_missing)


async def _attach_sheet_photos(sheet_id: str, tabs: list[ParsedTab]) -> None:
    """Lift the anchored photos out of the XLSX export onto the parsed items.

    A CSV cannot carry pictures, so the IMAGE column reads as empty even when
    the sheet is full of them. The XLSX export does carry them, anchored to a
    row, which is what `source_row` is for.

    Best effort: the export is large and may fail, and a menu import without
    photos is far better than no menu import.
    """
    try:
        workbook = await fetch_workbook(sheet_id)
    except IntegrationError as exc:
        log.warning("sheet_photos_unavailable", error=str(exc))
        return

    found = sheet_images.extract(workbook)
    if not found:
        return

    for index, tab in enumerate(tabs):
        if index >= len(found):
            break
        by_row = found[index].by_row
        stored = 0
        for item in tab.items:
            raw = by_row.get(item.source_row)
            if not raw:
                continue
            path = media.store_bytes(
                raw, key=f"{sheet_id}:{index}:{item.source_row}")
            if path:
                item.image_url = path
                stored += 1
        log.info("sheet_photos_attached", tab=found[index].name, stored=stored)


async def import_from_csv_text(session: AsyncSession, text: str, *,
                               cuisine: str = "",
                               deactivate_missing: bool = True) -> ImportResult:
    """Import a single uploaded CSV."""
    tab = parse_csv(text, fallback_cuisine=cuisine)
    return await apply(session, [tab], deactivate_missing=deactivate_missing)


def _tab_name_to_cuisine(tab_name: str) -> str:
    """'Chettinad US - Apr'26' -> 'Chettinad' (fallback if no title row)."""
    first = re.split(r"[-–(]", tab_name)[0].strip()
    first = re.sub(r"\b(US|USA|menu)\b", "", first, flags=re.IGNORECASE).strip()
    return first


async def apply(session: AsyncSession, tabs: list[ParsedTab], *,
                deactivate_missing: bool = True,
                fetch_images: bool = True) -> ImportResult:
    """Write parsed tabs into the database, idempotently."""
    result = ImportResult()

    # Pull every image once, up front, over one connection pool. Doing it per
    # row would re-open a connection 287 times; doing it at request time would
    # make the customer wait on somebody else's CDN.
    if fetch_images:
        # Anything already under /media is a photo we lifted out of the XLSX
        # moments ago. Only remote links need fetching; treating a local path
        # as a URL would fail and then blank the photo we just saved.
        remote = [item.image_url for tab in tabs for item in tab.items
                  if item.image_url
                  and not item.image_url.startswith(media.MEDIA_URL_PREFIX)]
        local_count = sum(1 for tab in tabs for item in tab.items
                          if item.image_url.startswith(media.MEDIA_URL_PREFIX))

        stored = await media.fetch_many(remote) if remote else {}
        result.images_stored = len(stored) + local_count
        result.images_failed = len({s for s in remote if s not in stored})

        for tab in tabs:
            for item in tab.items:
                if item.image_url.startswith(media.MEDIA_URL_PREFIX):
                    continue
                item.image_url = stored.get(item.image_url, "")

    for index, tab in enumerate(tabs):
        result.warnings.extend(tab.warnings)
        if not tab.cuisine or not tab.items:
            continue

        cuisine = await _upsert_cuisine(session, tab.cuisine, position=index)
        result.cuisines.append(cuisine.name)

        categories: dict[str, Category] = {}
        seen_retailer_ids: set[str] = set()

        for item in tab.items:
            category = categories.get(item.category)
            if category is None:
                category, created = await _upsert_category(
                    session, cuisine, item.category, position=len(categories)
                )
                categories[item.category] = category
                result.categories_created += int(created)

            retailer_id = build_retailer_id(cuisine.name, item.category, item.name)
            if retailer_id in seen_retailer_ids:
                result.warnings.append(
                    f"duplicate row '{item.name}' in {cuisine.name} / "
                    f"{item.category} - only the first was imported"
                )
                continue
            seen_retailer_ids.add(retailer_id)

            created = await _upsert_item(session, category, retailer_id, item)
            if created:
                result.items_created += 1
            else:
                result.items_updated += 1

        if deactivate_missing:
            result.items_deactivated += await _deactivate_missing(
                session, cuisine, seen_retailer_ids
            )

    await session.flush()
    log.info("menu_imported", summary=result.summary())
    return result


async def _upsert_cuisine(session: AsyncSession, name: str,
                          *, position: int) -> Cuisine:
    slug = slugify(name, max_length=40)
    found = await session.execute(select(Cuisine).where(Cuisine.slug == slug))
    cuisine = found.scalar_one_or_none()

    if cuisine is None:
        cuisine = Cuisine(name=name, slug=slug, position=position, is_active=True)
        session.add(cuisine)
        await session.flush()
    else:
        cuisine.name = name
        cuisine.is_active = True
    return cuisine


async def _upsert_category(session: AsyncSession, cuisine: Cuisine, name: str,
                           *, position: int) -> tuple[Category, bool]:
    slug = slugify(name, max_length=60)
    found = await session.execute(
        select(Category).where(Category.cuisine_id == cuisine.id,
                               Category.slug == slug)
    )
    category = found.scalar_one_or_none()

    if category is None:
        category = Category(cuisine_id=cuisine.id, name=name, slug=slug,
                            position=position, is_active=True)
        session.add(category)
        await session.flush()
        return category, True

    category.name = name
    category.position = position
    category.is_active = True
    return category, False


async def _upsert_item(session: AsyncSession, category: Category,
                       retailer_id: str, parsed: ParsedItem) -> bool:
    found = await session.execute(
        select(MenuItem).where(MenuItem.retailer_id == retailer_id)
    )
    item = found.scalar_one_or_none()
    pack_size, serves = extract_pack_details(parsed.description)

    if item is None:
        session.add(MenuItem(
            category_id=category.id,
            retailer_id=retailer_id,
            name=parsed.name,
            description=parsed.description,
            image_url=parsed.image_url or None,
            price=parsed.price,
            cost_price=parsed.cost_price,
            pack_size=pack_size,
            serves=serves,
            position=parsed.position,
            is_available=True,
        ))
        return True

    # Update in place so order history keeps pointing at the same row.
    item.category_id = category.id
    item.name = parsed.name
    item.description = parsed.description
    item.image_url = parsed.image_url or item.image_url
    item.price = parsed.price
    item.cost_price = parsed.cost_price
    item.pack_size = pack_size
    item.serves = serves
    item.position = parsed.position
    item.is_available = True
    return False


async def _deactivate_missing(session: AsyncSession, cuisine: Cuisine,
                              keep: set[str]) -> int:
    """Hide items that are no longer in the sheet, without deleting them."""
    found = await session.execute(
        select(MenuItem)
        .join(Category, MenuItem.category_id == Category.id)
        .where(Category.cuisine_id == cuisine.id,
               MenuItem.is_available.is_(True))
    )
    count = 0
    for item in found.scalars():
        if item.retailer_id not in keep:
            item.is_available = False
            count += 1
    return count
