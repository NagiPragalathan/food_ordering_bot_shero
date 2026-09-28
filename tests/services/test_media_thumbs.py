"""Card-sized photos, and browser caching for photos.

The menu page loads a photo per dish; these keep that cheap on a slow link.
"""

from __future__ import annotations

import io

import pytest
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.testclient import TestClient

from app.core.http_cache import CACHE_FOREVER, ImmutableStaticFiles
from app.services import media, media_thumbs

NAME = "0123456789abcdef.jpg"


@pytest.fixture
def photos(tmp_path, monkeypatch):
    """A media folder holding one large photo."""
    from PIL import Image

    monkeypatch.setattr(media, "MEDIA_DIR", tmp_path)
    monkeypatch.setattr(media_thumbs, "THUMB_DIR", tmp_path / "thumbs")
    buffer = io.BytesIO()
    Image.effect_noise((800, 800), 60).convert("RGB").save(buffer, "JPEG", quality=90)
    (tmp_path / NAME).write_bytes(buffer.getvalue())
    return tmp_path


def test_a_local_photo_gets_a_thumbnail_url():
    assert media_thumbs.thumb_url(f"/media/{NAME}") == f"/media/thumb/{NAME}"


@pytest.mark.parametrize("url", ["", None, "https://cdn.example/x.jpg",
                                 "/media/../secret.jpg", "/media/sub/0123456789abcdef.jpg"])
def test_anything_else_passes_through_untouched(url):
    assert media_thumbs.thumb_url(url) == (url or "")


def test_the_thumbnail_is_small_and_made_once(photos):
    from PIL import Image

    first = media_thumbs.thumbnail_file(NAME)
    assert first is not None and first.parent.name == "thumbs"
    with Image.open(first) as image:
        assert max(image.size) == media_thumbs.THUMB_PX
    assert first.stat().st_size < (photos / NAME).stat().st_size / 3

    made_at = first.stat().st_mtime_ns
    assert media_thumbs.thumbnail_file(NAME).stat().st_mtime_ns == made_at


@pytest.mark.parametrize("name", ["../../etc/passwd", "nothex.jpg", "0123456789abcdef.exe"])
def test_a_name_that_is_not_a_photo_is_refused(photos, name):
    assert media_thumbs.thumbnail_file(name) is None


def test_a_missing_photo_is_none(photos):
    assert media_thumbs.thumbnail_file("fedcba9876543210.jpg") is None


def test_an_unreadable_photo_falls_back_to_the_original(photos):
    broken = photos / "aaaaaaaaaaaaaaaa.jpg"
    broken.write_bytes(b"not an image")
    assert media_thumbs.thumbnail_file(broken.name) == broken


# --- HTTP ---------------------------------------------------------------------------
def test_photos_are_cached_by_the_browser_for_good(photos):
    app = FastAPI()
    app.mount("/media", ImmutableStaticFiles(directory=str(photos)))
    response = TestClient(app).get(f"/media/{NAME}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == CACHE_FOREVER


def test_the_thumbnail_route_serves_a_cacheable_jpeg(photos):
    from app.api.routes import media_thumbs as route

    app = FastAPI()
    app.include_router(route.router)
    client = TestClient(app)

    response = client.get(f"/media/thumb/{NAME}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == CACHE_FOREVER
    assert client.get("/media/thumb/fedcba9876543210.jpg").status_code == 404


def test_large_json_is_compressed():
    app = FastAPI()
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    @app.get("/menu")
    def menu():
        return {"items": [{"name": f"Dish {i}", "price": 9.5} for i in range(500)]}

    response = TestClient(app).get("/menu", headers={"Accept-Encoding": "gzip"})
    assert response.headers.get("content-encoding") == "gzip"
