"""Bulk price update: the PPP (Triple-P, the partner purchase price) and,
when asked, the MRP (selling price) of dishes already on the menu, from a
pricing sheet.

Unlike the menu import it never adds, renames or hides a dish: each sheet row
is matched to a dish of the same cuisine on this business's menu, and only its
prices change. So it works over any menu, whether it came from a Google Sheet
(Shero US) or the ERP's exports (DST India, whose dishes carry ERP item codes).

Any tab with a header row holding "PPP" and "MRP" is read, wherever those
columns are:

    Shero US:   # | Sambar        | Description | Image | PPP   | MRP
    DST India:  # | # | Chettinad Special | PPP | MRP | Description | Image

The category name sits just before the first label, and that column holds
each dish's name below it. The cuisine is the tab's title row ("Chettinad
Cuisine - ..."), else the tab's name. Tabs without such rows (a key, an
index, an SOP) are skipped, as are tabs named "... Before ..." (old prices);
when two tabs give one cuisine, the later one is used.

Matching, inside the cuisine:
  1. the name, ignoring case, spaces and punctuation; a dish listed under
     two categories on the menu gets the price on both;
  2. else the name without its size ("Medu Vada - 8 Nos ( No Chutney )" is
     the menu's "Medu Vada ( No Chutney )"), only onto a menu dish whose own
     name has no size - so "Pongal - 750 ml" never lands on "Pongal - 450 ml"
     - and only when exactly one dish and one sheet row have it.
Anything else is listed as not found and left alone: a price never lands on
a guess.

`plan()` works out every change without writing (the Check step);
`apply()` writes the plan.
"""

from __future__ import annotations

import csv
import io
import json
import re
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import locale
from app.core.logging import get_logger
from app.db.models import Category, Cuisine, MenuItem
from app.services import menu_import

log = get_logger(__name__)

MAX_ROWS = 20_000
# The size in a dish's name: "Beans Sambar - 16 OZ", "Medu Vada - 8 Nos
# ( No Chutney )", "Moor Kuzhambu -16 OZ". Without it, the dish's own name.
SIZE_RE = re.compile(r"\s*-\s*\d+(?:\.\d+)?\s*(?:oz|ml|nos|no|pcs|pieces|kg|g|ltr|l)\b\.?",
                     re.IGNORECASE)
LABELS = ("ppp", "mrp", "description", "image")


class PriceSheetError(ValueError):
    """A sheet or file this cannot read; the message says why, for the page."""


@dataclass
class SheetRow:
    cuisine: str
    category: str
    name: str
    ppp: Decimal | None
    mrp: Decimal | None
    tab: str
    row: int


@dataclass
class Change:
    item_id: str
    cuisine: str
    name: str
    old_ppp: Decimal | None
    new_ppp: Decimal | None
    old_mrp: Decimal
    new_mrp: Decimal


@dataclass
class PricePlan:
    update_mrp: bool = False
    tabs_used: list[str] = field(default_factory=list)
    tabs_skipped: list[str] = field(default_factory=list)
    rows: int = 0                     # sheet dishes read
    matched: int = 0                  # sheet dishes found on the menu
    changes: list[Change] = field(default_factory=list)
    unchanged: int = 0                # menu dishes already at the sheet's prices
    not_found: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ppp_changes(self) -> int:
        return sum(1 for c in self.changes if c.new_ppp != c.old_ppp)

    @property
    def mrp_changes(self) -> int:
        return sum(1 for c in self.changes if c.new_mrp != c.old_mrp)

    def summary(self) -> str:
        parts = [f"{self.matched} of {self.rows} sheet dishes found on the menu",
                 f"PPP changed on {self.ppp_changes} dish(es)"]
        if self.update_mrp:
            parts.append(f"MRP changed on {self.mrp_changes}")
        parts.append(f"{self.unchanged} already right")
        if self.not_found:
            parts.append(f"{len(self.not_found)} not found")
        if self.conflicts:
            parts.append(f"{len(self.conflicts)} left alone (listed twice with different prices)")
        return ", ".join(parts)


# --- reading -------------------------------------------------------------------
def parse_tab(rows: list[list[str]], tab: str) -> list[SheetRow]:
    """One tab's dishes with their PPP and MRP (header-label layout)."""
    cuisine = ""
    columns: dict[str, int] | None = None
    category = ""
    found: list[SheetRow] = []
    for number, raw in enumerate(rows[:MAX_ROWS], start=1):
        cells = [_text(c) for c in raw]
        low = [c.lower() for c in cells]
        if not cuisine:
            cuisine = next((menu_import._cuisine_from_title(c) for c in cells
                            if menu_import._cuisine_from_title(c)), "")
        ppp = next((i for i, c in enumerate(low) if c.startswith("ppp")), None)
        mrp = next((i for i, c in enumerate(low) if c.startswith("mrp")), None)
        if ppp is not None and mrp is not None:
            first = min(i for i, c in enumerate(low) if c.startswith(LABELS))
            name = next((i for i in range(first - 1, -1, -1)
                         if cells[i] and not _is_number(cells[i])), None)
            if name is None:
                continue
            columns = {"name": name, "ppp": ppp, "mrp": mrp}
            category = cells[name]
            continue
        if columns is None:
            continue
        dish = _cell(cells, columns["name"])
        price = _money(_cell(cells, columns["mrp"]))
        if not dish or price is None:
            continue
        found.append(SheetRow(cuisine=cuisine or _cuisine_from_tab(tab), category=category,
                              name=" ".join(dish.split()), ppp=_money(_cell(cells, columns["ppp"])),
                              mrp=price, tab=tab, row=number))
    return found


def read_tabs(tabs: list[tuple[str, list[list[str]]]], plan: PricePlan) -> list[SheetRow]:
    """Every usable tab's dishes: old-price tabs and tabs without prices skipped,
    and the later tab kept when two give the same cuisine."""
    by_cuisine: dict[str, tuple[str, list[SheetRow]]] = {}
    for name, rows in tabs:
        if re.search(r"\bbefore\b", name, re.IGNORECASE):
            plan.tabs_skipped.append(f"{name} (old prices)")
            continue
        dishes = parse_tab(rows, name)
        if not dishes:
            plan.tabs_skipped.append(f"{name} (no PPP / MRP rows)")
            continue
        cuisine = dishes[0].cuisine.lower()
        if cuisine in by_cuisine:
            plan.tabs_skipped.append(f"{by_cuisine[cuisine][0]} (a later tab has "
                                     f"{dishes[0].cuisine})")
        by_cuisine[cuisine] = (name, dishes)
    plan.tabs_used = [name for name, _ in by_cuisine.values()]
    return [row for _, dishes in by_cuisine.values() for row in dishes]


def csv_rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def workbook_tabs(data: bytes) -> list[tuple[str, list[list[str]]]]:
    """An uploaded .xlsx as (tab name, rows)."""
    from openpyxl import load_workbook

    try:
        book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - any unreadable file
        raise PriceSheetError("Could not open that Excel file. Save it as .xlsx and "
                              "try again.") from exc
    try:
        return [(sheet.title, [[_text(v) for v in row]
                               for row in sheet.iter_rows(values_only=True)])
                for sheet in book.worksheets]
    finally:
        book.close()


async def sheet_tabs(sheet_url: str) -> list[tuple[str, list[list[str]]]]:
    """Every tab of a Google Sheet shared for viewing, as (tab name, rows)."""
    sheet_id = menu_import.sheet_id_from_url(sheet_url)
    return [(name, csv_rows(await menu_import.fetch_tab_csv(sheet_id, gid)))
            for name, gid in await menu_import.discover_tabs(sheet_id)]


# --- matching ------------------------------------------------------------------
async def plan(session: AsyncSession, rows: list[SheetRow], *,
               update_mrp: bool = False, plan_: PricePlan | None = None) -> PricePlan:
    """What the sheet would change on this business's menu. Writes nothing."""
    result = plan_ or PricePlan()
    result.update_mrp = update_mrp
    result.rows = len(rows)
    dishes = (await session.execute(
        select(MenuItem, Cuisine.name)
        .join(Category, MenuItem.category_id == Category.id)
        .join(Cuisine, Category.cuisine_id == Cuisine.id))).all()
    exact: dict[tuple[str, str], list[MenuItem]] = defaultdict(list)
    base: dict[tuple[str, str], list[MenuItem]] = defaultdict(list)
    names: dict[str, str] = {}
    cuisines = set()
    for item, cuisine in dishes:
        c = cuisine.lower()
        cuisines.add(c)
        names[str(item.id)] = cuisine
        exact[(c, _key(item.name))].append(item)
        if not SIZE_RE.search(item.name or ""):
            base[(c, _base(item.name))].append(item)
    sheet_bases: dict[tuple[str, str], int] = defaultdict(int)
    for row in rows:
        sheet_bases[(row.cuisine.lower(), _base(row.name))] += 1

    wanted: dict[str, tuple[MenuItem, Decimal | None, Decimal, list[SheetRow]]] = {}
    clashing: set[str] = set()
    missing_cuisines: set[str] = set()
    for row in rows:
        c = row.cuisine.lower()
        if c not in cuisines:
            missing_cuisines.add(row.cuisine)
            continue
        hits = exact.get((c, _key(row.name))) or []
        if not hits:
            one = base.get((c, _base(row.name))) or []
            if len(one) == 1 and sheet_bases[(c, _base(row.name))] == 1:
                hits = one
        if not hits:
            result.not_found.append(f"{row.cuisine} / {row.category} / {row.name}")
            continue
        result.matched += 1
        for item in hits:
            key = str(item.id)
            before = wanted.get(key)
            if before is None:
                wanted[key] = (item, row.ppp, row.mrp, [row])
                continue
            before[3].append(row)
            if (before[1], before[2]) != (row.ppp, row.mrp):
                clashing.add(key)
    for cuisine in sorted(missing_cuisines):
        result.warnings.append(f"This menu has no cuisine called {cuisine}: its tab was "
                               "left alone.")

    for key, (item, ppp, mrp, sources) in wanted.items():
        if key in clashing:
            result.conflicts.append(
                f"{names[key]} / {item.name} (sheet rows "
                f"{', '.join(f'{r.tab} {r.row}' for r in sources)})")
            continue
        new_ppp = ppp if ppp is not None else item.cost_price
        new_mrp = mrp if update_mrp and mrp else item.price
        old_ppp = _round(item.cost_price)
        if _round(new_ppp) == old_ppp and _round(new_mrp) == _round(item.price):
            result.unchanged += 1
            continue
        if new_ppp is not None and new_mrp is not None and new_ppp > new_mrp:
            result.warnings.append(f"{names[key]} / {item.name}: PPP "
                                   f"{locale.money(new_ppp)} is above the MRP "
                                   f"{locale.money(new_mrp)}")
        result.changes.append(Change(item_id=key, cuisine=names[key], name=item.name,
                                     old_ppp=old_ppp, new_ppp=_round(new_ppp),
                                     old_mrp=_round(item.price), new_mrp=_round(new_mrp)))
    return result


async def apply(session: AsyncSession, result: PricePlan) -> int:
    """Write the planned prices. Returns how many dishes changed."""
    if not result.changes:
        return 0
    ids = {c.item_id: c for c in result.changes}
    items = (await session.execute(
        select(MenuItem).where(MenuItem.id.in_([uuid.UUID(i) for i in ids])))).scalars()
    changed = 0
    for item in items:
        change = ids.get(str(item.id))
        if change is None:
            continue
        item.cost_price = change.new_ppp
        if result.update_mrp and change.new_mrp:
            item.price = change.new_mrp
        changed += 1
    await session.flush()
    log.info("menu_prices_updated", changed=changed, mrp=result.update_mrp,
             summary=result.summary())
    return changed


# --- carrying the sheet from the Check step to the Update step -----------------
def dump_rows(rows: list[SheetRow], plan_: PricePlan) -> str:
    return json.dumps({"rows": [{**asdict(r), "ppp": _str(r.ppp), "mrp": _str(r.mrp)}
                                for r in rows],
                       "used": plan_.tabs_used, "skipped": plan_.tabs_skipped},
                      separators=(",", ":"))


def load_rows(payload: str, plan_: PricePlan) -> list[SheetRow]:
    try:
        data = json.loads(payload)
        plan_.tabs_used = [str(t) for t in data.get("used", [])]
        plan_.tabs_skipped = [str(t) for t in data.get("skipped", [])]
        return [SheetRow(cuisine=str(r["cuisine"]), category=str(r["category"]),
                         name=str(r["name"]), ppp=_money(r.get("ppp") or ""),
                         mrp=_money(r.get("mrp") or ""), tab=str(r["tab"]),
                         row=int(r["row"]))
                for r in data["rows"][:MAX_ROWS]]
    except (ValueError, KeyError, TypeError) as exc:
        raise PriceSheetError("That check has expired: check the sheet again.") from exc


# --- helpers -------------------------------------------------------------------
def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _base(name: str) -> str:
    return _key(SIZE_RE.sub("", name or ""))


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _cell(cells: list[str], index: int) -> str:
    return cells[index] if index < len(cells) else ""


def _is_number(text: str) -> bool:
    return bool(re.fullmatch(r"\d+(\.\d+)?", text.strip()))


def _money(text: str) -> Decimal | None:
    cleaned = locale.amount_text(text or "")
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    return value if value >= 0 else None


def _round(value) -> Decimal | None:
    return None if value is None else Decimal(str(value)).quantize(Decimal("0.01"))


def _str(value: Decimal | None) -> str:
    return "" if value is None else str(value)


def _cuisine_from_tab(tab: str) -> str:
    return menu_import._tab_name_to_cuisine(re.sub(r"\b(after|before)\b.*$", "", tab,
                                                   flags=re.IGNORECASE))
