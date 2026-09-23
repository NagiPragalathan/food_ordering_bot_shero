"""Pull the dish photos out of a Google Sheet.

A CSV export cannot carry pictures, which is why the IMAGE column looks empty:
the photos are anchored *over* the cells, not stored in them. They do survive
an **XLSX** export, as files under `xl/media/` plus a drawing part per sheet
that says which cell each one sits on.

So this reads the XLSX and returns, per tab, a map of `row number -> image
bytes`. The importer matches those rows to the dishes it parsed from the same
tab, giving each dish its photo.

Anchor rows are 0-based in the file and 1-based when read as CSV lines, so
they are converted once here rather than at every call site.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field

from app.core.logging import get_logger

log = get_logger(__name__)

# <xdr:from><xdr:col>4</xdr:col>…<xdr:row>3</xdr:row></xdr:from> … r:embed="rId7"
_ANCHOR = re.compile(
    r"<xdr:(?:one|two)CellAnchor.*?"
    r"<xdr:col>(\d+)</xdr:col>.*?<xdr:row>(\d+)</xdr:row>.*?"
    r'r:embed="([^"]+)"',
    re.DOTALL,
)
_RELATION = re.compile(r'Id="([^"]+)"[^>]*Target="([^"]+)"')
_SHEET_NAME = re.compile(r'<sheet[^>]*name="([^"]*)"[^>]*r:id="([^"]*)"')
_WB_RELATION = re.compile(r'Id="([^"]+)"[^>]*Target="([^"]+)"')


@dataclass
class TabImages:
    """Photos found on one worksheet, keyed by 1-based row number."""

    name: str
    by_row: dict[int, bytes] = field(default_factory=dict)


def extract(xlsx_bytes: bytes) -> list[TabImages]:
    """Read every worksheet's anchored images, in workbook order.

    Returns an empty list rather than raising when the file is not a workbook
    or holds no pictures - a menu import must survive a sheet with no photos.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(xlsx_bytes))
    except zipfile.BadZipFile:
        log.warning("sheet_images_not_a_workbook")
        return []

    names = set(archive.namelist())
    tabs: list[TabImages] = []

    for index, (title, sheet_path) in enumerate(_worksheets(archive, names), start=1):
        tab = TabImages(name=title or f"Sheet{index}")
        drawing = _drawing_for(archive, names, sheet_path)
        if drawing:
            tab.by_row = _images_from_drawing(archive, names, drawing)
        tabs.append(tab)
        log.info("sheet_images_tab", tab=tab.name, images=len(tab.by_row))

    return tabs


def _worksheets(archive: zipfile.ZipFile, names: set[str]) -> list[tuple[str, str]]:
    """(title, path) for each worksheet, in the order the workbook lists them."""
    if "xl/workbook.xml" not in names:
        return []

    workbook = archive.read("xl/workbook.xml").decode("utf-8", "replace")
    relations = {}
    if "xl/_rels/workbook.xml.rels" in names:
        rels_xml = archive.read("xl/_rels/workbook.xml.rels").decode("utf-8", "replace")
        relations = {rid: target for rid, target in _WB_RELATION.findall(rels_xml)}

    sheets = []
    for title, rid in _SHEET_NAME.findall(workbook):
        target = relations.get(rid, "")
        if not target:
            continue
        path = target if target.startswith("xl/") else "xl/" + target.lstrip("/")
        if path in names:
            sheets.append((title, path))
    return sheets


def _drawing_for(archive: zipfile.ZipFile, names: set[str],
                 sheet_path: str) -> str | None:
    """The drawing part a worksheet points at, if it has one."""
    rels_path = sheet_path.replace("xl/worksheets/", "xl/worksheets/_rels/") + ".rels"
    if rels_path not in names:
        return None

    rels_xml = archive.read(rels_path).decode("utf-8", "replace")
    for _, target in _RELATION.findall(rels_xml):
        if "drawing" in target:
            path = target.replace("../", "xl/")
            return path if path in names else None
    return None


def _images_from_drawing(archive: zipfile.ZipFile, names: set[str],
                         drawing_path: str) -> dict[int, bytes]:
    """Row number -> image bytes for one drawing part."""
    drawing_xml = archive.read(drawing_path).decode("utf-8", "replace")

    rels_path = drawing_path.replace("xl/drawings/", "xl/drawings/_rels/") + ".rels"
    if rels_path not in names:
        return {}
    rels_xml = archive.read(rels_path).decode("utf-8", "replace")
    targets = {rid: target for rid, target in _RELATION.findall(rels_xml)}

    found: dict[int, bytes] = {}
    for _col, row, rid in _ANCHOR.findall(drawing_xml):
        target = targets.get(rid)
        if not target:
            continue
        media_path = target.replace("../", "xl/")
        if media_path not in names:
            continue

        # Anchors count rows from 0; the CSV parser counts lines from 1.
        row_number = int(row) + 1
        if row_number in found:
            continue   # first picture on a row wins
        try:
            found[row_number] = archive.read(media_path)
        except KeyError:
            continue

    return found
