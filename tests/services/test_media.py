"""Dish image fetching.

The IMAGE column is somebody else's link, in whatever form the sheet happens
to hold it. The job here is to turn that into a fetchable URL, store the file
once, and never let a bad image break a menu import.
"""

from __future__ import annotations

import httpx
import pytest

from app.services import media


class TestNormaliseSource:
    def test_a_plain_url_passes_through(self):
        assert media.normalise_source("https://cdn.test/a.jpg") == "https://cdn.test/a.jpg"

    def test_surrounding_whitespace_is_trimmed(self):
        assert media.normalise_source("  https://cdn.test/a.jpg  ") == "https://cdn.test/a.jpg"

    @pytest.mark.parametrize("cell", [
        '=IMAGE("https://cdn.test/a.jpg")',
        "=image('https://cdn.test/a.jpg')",
        '=IMAGE( "https://cdn.test/a.jpg" , 1 )',
    ])
    def test_a_sheets_image_formula_is_unwrapped(self, cell):
        """Sheets stores photos as a formula, not as a bare URL."""
        assert media.normalise_source(cell) == "https://cdn.test/a.jpg"

    @pytest.mark.parametrize("cell", [
        "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view?usp=sharing",
        "https://drive.google.com/open?id=1AbCdEfGhIjKlMnOp",
    ])
    def test_a_drive_link_becomes_a_direct_download(self, cell):
        """A Drive share link serves an HTML preview, not the image."""
        assert media.normalise_source(cell) == (
            "https://drive.google.com/uc?export=download&id=1AbCdEfGhIjKlMnOp"
        )

    @pytest.mark.parametrize("cell", ["", "   ", "not a url", "photo.jpg", None])
    def test_anything_unusable_becomes_empty(self, cell):
        assert media.normalise_source(cell) == ""


def _respond(monkeypatch, *, status=200, content_type="image/jpeg", body=b"\xff\xd8data"):
    async def get(self, url, **kwargs):
        return httpx.Response(status, content=body,
                              headers={"content-type": content_type},
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx.AsyncClient, "get", get)


@pytest.fixture
def media_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "MEDIA_DIR", tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_a_fetched_image_is_written_and_served_from_our_own_path(
        media_dir, monkeypatch):
    _respond(monkeypatch)
    path = await media.fetch("https://cdn.test/a.jpg")

    assert path.startswith("/media/")
    assert len(list(media_dir.iterdir())) == 1


@pytest.mark.asyncio
async def test_the_same_url_is_not_downloaded_twice(media_dir, monkeypatch):
    """Re-importing 287 rows must not re-fetch every photo."""
    calls = []

    async def get(self, url, **kwargs):
        calls.append(url)
        return httpx.Response(200, content=b"x",
                              headers={"content-type": "image/png"},
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx.AsyncClient, "get", get)

    first = await media.fetch("https://cdn.test/a.png")
    second = await media.fetch("https://cdn.test/a.png")

    assert first == second
    assert len(calls) == 1, "the second call should have used the stored file"


@pytest.mark.asyncio
async def test_an_http_error_returns_none_rather_than_raising(media_dir, monkeypatch):
    """A dead link must not fail the whole menu import."""
    _respond(monkeypatch, status=404)
    assert await media.fetch("https://cdn.test/missing.jpg") is None


@pytest.mark.asyncio
async def test_a_network_failure_returns_none(media_dir, monkeypatch):
    async def get(self, url, **kwargs):
        raise httpx.ConnectError("no route")
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    assert await media.fetch("https://cdn.test/a.jpg") is None


@pytest.mark.asyncio
async def test_html_is_rejected_rather_than_saved_as_an_image(media_dir, monkeypatch):
    """What a Drive interstitial returns - saving it would show a broken card."""
    _respond(monkeypatch, content_type="text/html", body=b"<html>sign in</html>")
    assert await media.fetch("https://drive.google.com/uc?id=abc1234567") is None
    assert list(media_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_an_oversized_file_is_rejected(media_dir, monkeypatch):
    _respond(monkeypatch, body=b"x" * (media.MAX_BYTES + 1))
    assert await media.fetch("https://cdn.test/huge.jpg") is None
    assert list(media_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_a_blank_cell_never_reaches_the_network(media_dir, monkeypatch):
    async def get(self, url, **kwargs):
        raise AssertionError("should not have made a request")
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    assert await media.fetch("") is None


@pytest.mark.asyncio
async def test_a_path_we_already_own_is_returned_unchanged(media_dir, monkeypatch):
    async def get(self, url, **kwargs):
        raise AssertionError("should not re-fetch our own file")
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    assert await media.fetch("/media/abc.jpg") is None  # not an http URL


@pytest.mark.asyncio
async def test_fetch_many_maps_each_source_to_its_stored_path(media_dir, monkeypatch):
    _respond(monkeypatch)
    stored = await media.fetch_many([
        "https://cdn.test/a.jpg",
        "https://cdn.test/b.jpg",
        "https://cdn.test/a.jpg",   # duplicate
        "",                          # blank cell
    ])
    assert set(stored) == {"https://cdn.test/a.jpg", "https://cdn.test/b.jpg"}


@pytest.mark.asyncio
async def test_fetch_many_omits_failures_instead_of_raising(media_dir, monkeypatch):
    _respond(monkeypatch, status=500)
    assert await media.fetch_many(["https://cdn.test/a.jpg"]) == {}
