"""Reading dish photos out of an XLSX export.

The client's sheet shows a picture beside every dish, but the CSV export has
an empty IMAGE column: the photos are anchored over the cells rather than
stored in them. They do survive the XLSX export, so this reads them from
there and maps each one back to its row.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.services import media, sheet_images

WORKBOOK_XML = (
    '<workbook xmlns:r="x">'
    '<sheets><sheet name="Chettinad" r:id="rId1"/>'
    '<sheet name="Kerala" r:id="rId2"/></sheets></workbook>'
)
WORKBOOK_RELS = (
    '<Relationships>'
    '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
    '<Relationship Id="rId2" Target="worksheets/sheet2.xml"/>'
    '</Relationships>'
)
SHEET_RELS = ('<Relationships><Relationship Id="rId9" '
              'Target="../drawings/drawing1.xml"/></Relationships>')
DRAWING_RELS = ('<Relationships><Relationship Id="rIdA" '
                'Target="../media/image1.png"/><Relationship Id="rIdB" '
                'Target="../media/image2.png"/></Relationships>')


def _anchor(row: int, rid: str, col: int = 4) -> str:
    return (
        f"<xdr:oneCellAnchor><xdr:from><xdr:col>{col}</xdr:col>"
        f"<xdr:colOff>0</xdr:colOff><xdr:row>{row}</xdr:row>"
        f"<xdr:rowOff>0</xdr:rowOff></xdr:from>"
        f'<xdr:pic><xdr:blipFill><a:blip r:embed="{rid}"/></xdr:blipFill>'
        f"</xdr:pic></xdr:oneCellAnchor>"
    )


def _workbook(drawing_body: str, *, media_files: dict[str, bytes] | None = None,
              with_drawing: bool = True) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", WORKBOOK_XML)
        archive.writestr("xl/_rels/workbook.xml.rels", WORKBOOK_RELS)
        archive.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")
        archive.writestr("xl/worksheets/sheet2.xml", "<worksheet/>")
        if with_drawing:
            archive.writestr("xl/worksheets/_rels/sheet1.xml.rels", SHEET_RELS)
            archive.writestr("xl/drawings/drawing1.xml",
                             "<xdr:wsDr>" + drawing_body + "</xdr:wsDr>")
            archive.writestr("xl/drawings/_rels/drawing1.xml.rels", DRAWING_RELS)
        for name, data in (media_files or {}).items():
            archive.writestr(f"xl/media/{name}", data)
    return buffer.getvalue()


def test_an_anchored_image_is_returned_against_its_row():
    """Anchors count rows from 0; a CSV reader counts lines from 1."""
    data = _workbook(_anchor(3, "rIdA"), media_files={"image1.png": b"PNGDATA"})
    tabs = sheet_images.extract(data)

    assert [t.name for t in tabs] == ["Chettinad", "Kerala"]
    assert tabs[0].by_row == {4: b"PNGDATA"}


def test_several_rows_each_keep_their_own_image():
    data = _workbook(_anchor(3, "rIdA") + _anchor(7, "rIdB"),
                     media_files={"image1.png": b"A", "image2.png": b"B"})
    assert sheet_images.extract(data)[0].by_row == {4: b"A", 8: b"B"}


def test_the_first_image_on_a_row_wins():
    """A stray second picture on one row must not replace the dish photo."""
    data = _workbook(_anchor(3, "rIdA") + _anchor(3, "rIdB"),
                     media_files={"image1.png": b"A", "image2.png": b"B"})
    assert sheet_images.extract(data)[0].by_row == {4: b"A"}


def test_a_sheet_with_no_drawing_simply_has_no_images():
    data = _workbook("", with_drawing=False)
    tabs = sheet_images.extract(data)
    assert [t.by_row for t in tabs] == [{}, {}]


def test_an_anchor_pointing_at_a_missing_file_is_skipped():
    """A malformed workbook must not raise mid-import."""
    data = _workbook(_anchor(3, "rIdA"), media_files={})
    assert sheet_images.extract(data)[0].by_row == {}


def test_something_that_is_not_a_workbook_returns_nothing():
    assert sheet_images.extract(b"this is not a zip file") == []


def test_an_empty_payload_returns_nothing():
    assert sheet_images.extract(b"") == []


# --- storing what was extracted ----------------------------------------------
@pytest.fixture
def media_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "MEDIA_DIR", tmp_path)
    return tmp_path


def _png(width: int = 1600, height: int = 1200) -> bytes:
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGBA", (width, height), (200, 60, 40, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_a_stored_photo_is_downscaled(media_dir):
    """The sheet's originals are ~850KB; a phone menu cannot carry 287 of those."""
    from PIL import Image

    original = _png()
    path = media.store_bytes(original, key="sheet:0:4")
    saved = media_dir / path.rsplit("/", 1)[-1]

    assert saved.stat().st_size < len(original)
    with Image.open(saved) as image:
        assert max(image.size) <= media.THUMBNAIL_MAX_PX
        assert image.mode == "RGB", "JPEG cannot carry an alpha channel"


def test_the_same_source_keeps_the_same_filename(media_dir):
    """Re-importing overwrites rather than filling the disk with duplicates."""
    first = media.store_bytes(_png(200, 200), key="sheet:0:4")
    second = media.store_bytes(_png(200, 200), key="sheet:0:4")
    assert first == second
    assert len(list(media_dir.iterdir())) == 1


def test_different_rows_get_different_files(media_dir):
    a = media.store_bytes(_png(200, 200), key="sheet:0:4")
    b = media.store_bytes(_png(200, 200), key="sheet:0:5")
    assert a != b


def test_bytes_that_are_not_an_image_are_rejected(media_dir):
    assert media.store_bytes(b"not an image", key="sheet:0:4") is None
    assert list(media_dir.iterdir()) == []


def test_empty_bytes_are_rejected(media_dir):
    assert media.store_bytes(b"", key="sheet:0:4") is None
