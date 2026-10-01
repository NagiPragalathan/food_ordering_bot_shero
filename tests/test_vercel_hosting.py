"""Running on Vercel (docs/vercel-hosting.md): hosted database URLs, the cron
endpoints, and dish photos in Vercel Blob."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db.url import driver_url
from app.integrations import vercel_blob
from app.services import media, media_thumbs
from app.workers import cron

TOKEN = "vercel_blob_rw_StoreAbc123_secretvalue"
BLOB = "https://storeabc123.public.blob.vercel-storage.com"
NAME = "0123456789abcdef.jpg"


# --- database URL ---------------------------------------------------------------------
@pytest.mark.parametrize("given,expected", [
    ("postgres://u:p@host.neon.tech/db?sslmode=require&channel_binding=require",
     "postgresql+asyncpg://u:p@host.neon.tech/db?ssl=require"),
    ("postgresql://u:p@host/db", "postgresql+asyncpg://u:p@host/db"),
    ("postgresql+asyncpg://u:p@host/db?ssl=require", "postgresql+asyncpg://u:p@host/db?ssl=require"),
    ("sqlite+aiosqlite:///./shero_local.db", "sqlite+aiosqlite:///./shero_local.db"),
])
def test_hosted_database_urls_become_asyncpg_urls(given, expected):
    assert driver_url(given) == expected


# --- cron ---------------------------------------------------------------------------------
def test_the_payment_jobs_run_every_minute_and_the_rest_every_fifth():
    at = lambda minute: datetime(2026, 10, 1, 12, minute, tzinfo=timezone.utc)  # noqa: E731
    assert cron.due(at(1)) == cron.EVERY_MINUTE
    assert cron.due(at(5)) == cron.EVERY_MINUTE + cron.EVERY_5_MINUTES
    assert cron.due(at(7), daily=True) == cron.DAILY


async def test_one_failing_job_does_not_stop_the_others(monkeypatch):
    async def fine():
        return 3

    async def broken():
        raise RuntimeError("zoho down")

    monkeypatch.setattr(cron.jobs, "ALL_JOBS", {"a": broken, "b": fine})
    results = await cron.run(("a", "b"))
    assert results["b"] == 3 and str(results["a"]).startswith("error")


@pytest.fixture
def cron_client(monkeypatch):
    from app.api.routes import cron as route

    calls = []

    async def tick(now=None, *, daily=False):
        calls.append(daily)
        return {"payment_reminders": 0}

    monkeypatch.setattr(route.cron, "tick", tick)
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app), calls


def test_cron_needs_the_secret(cron_client, monkeypatch):
    client, calls = cron_client
    monkeypatch.setattr(settings, "cron_secret", "s3cret")

    assert client.get("/cron/tick").status_code == 401
    assert client.get("/cron/tick", headers={"authorization": "Bearer wrong"}).status_code == 401
    ok = client.get("/cron/tick", headers={"authorization": "Bearer s3cret"})
    assert ok.status_code == 200 and ok.json()["ok"] is True
    assert client.get("/cron/daily", headers={"authorization": "Bearer s3cret"}).status_code == 200
    assert calls == [False, True]


def test_cron_is_closed_to_everyone_without_a_secret(cron_client, monkeypatch):
    client, calls = cron_client
    monkeypatch.setattr(settings, "cron_secret", "")
    assert client.get("/cron/tick", headers={"authorization": "Bearer "}).status_code == 503
    assert calls == []


# --- Vercel Blob --------------------------------------------------------------------------
@pytest.fixture
def blob(monkeypatch):
    """Vercel Blob on, with uploads captured instead of sent."""
    monkeypatch.setattr(settings, "blob_read_write_token", TOKEN)
    puts: list[dict] = []

    def put(url, *, params, content, headers, timeout):
        puts.append({"pathname": params["pathname"], "headers": headers, "size": len(content)})
        return httpx.Response(200, json={"url": f"{BLOB}/{params['pathname']}"},
                              request=httpx.Request("PUT", url))
    monkeypatch.setattr(vercel_blob.httpx, "put", put)
    return puts


def _jpeg() -> bytes:
    import io

    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (900, 600), (200, 120, 40)).save(buffer, format="JPEG")
    return buffer.getvalue()


def test_a_photo_and_its_thumbnail_go_to_blob(blob):
    url = media.save(NAME, _jpeg(), "image/jpeg")

    assert url == f"{BLOB}/media/{NAME}"
    assert [p["pathname"] for p in blob] == [f"media/{NAME}", "media/thumbs/0123456789abcdef.jpg"]
    headers = blob[0]["headers"]
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["x-vercel-blob-store-id"] == "StoreAbc123"
    assert headers["x-add-random-suffix"] == "0" and headers["x-allow-overwrite"] == "1"
    # The card shows the uploaded thumbnail.
    assert media_thumbs.thumb_url(url) == f"{BLOB}/media/thumbs/0123456789abcdef.jpg"


def test_a_refused_upload_leaves_no_photo_rather_than_failing(monkeypatch):
    monkeypatch.setattr(settings, "blob_read_write_token", TOKEN)
    monkeypatch.setattr(vercel_blob.httpx, "put", lambda url, **kw: httpx.Response(
        403, json={"error": {"code": "forbidden"}}, request=httpx.Request("PUT", url)))
    assert media.save(NAME, _jpeg(), "image/jpeg") is None


def test_a_blob_photo_is_recognised_as_ours():
    assert media.is_ours(f"{BLOB}/media/{NAME}")
    assert media.is_ours(f"/media/{NAME}")
    assert not media.is_ours("https://drive.google.com/file/d/abc")


def test_without_blob_photos_stay_on_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "blob_read_write_token", "")
    monkeypatch.setattr(media, "MEDIA_DIR", tmp_path)
    assert media.save(NAME, b"jpeg-bytes", "image/jpeg") == f"/media/{NAME}"
    assert (tmp_path / NAME).read_bytes() == b"jpeg-bytes"


def test_serverless_writes_photos_under_tmp(monkeypatch):
    monkeypatch.setattr(settings, "vercel", "1")
    monkeypatch.setattr(settings, "media_dir", "")
    assert str(media._media_dir()).replace("\\", "/") == "/tmp/shero-media"
