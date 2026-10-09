"""Copy a SQLite database into MySQL (the AWS server) or a hosted Postgres (Vercel).

    python -m scripts.copy_database --to "mysql://user:pass@host:3306/db"
    python -m scripts.copy_database --to "postgres://user:pass@host/db?sslmode=require"

  1. Creates the tables in the target (alembic upgrade head).
  2. Copies every row: menu, kitchens, customers, orders, saved settings,
     admin logins - so the hosted bot carries on exactly where this one is.
  3. With --blob-token (or BLOB_READ_WRITE_TOKEN set), uploads each dish
     photo, and its card thumbnail, to Vercel Blob and points the dish at it,
     because a Vercel Function cannot serve files from data/media/.

Refuses to write into a database that already has data, unless --replace
(which empties it first). For Postgres use the direct (non-pooled) URL.
The source is read only.

The copied saved settings (Zoho connection and the rest) are encrypted with
SETTINGS_ENCRYPTION_KEY, so the new server must be given the same value as this
machine's .env.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.db.models import Base
from app.db.url import driver_url
from app.services import media

ROOT = Path(__file__).resolve().parent.parent
BATCH = 500


def _migrate(target_url: str) -> None:
    """Build the schema in the target with the project's own migrations."""
    env = {**os.environ, "DATABASE_URL": target_url}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                   cwd=ROOT, env=env, check=True)


def _aware(row: dict) -> dict:
    """SQLite hands back naive datetimes; they were stored as UTC."""
    return {key: (value.replace(tzinfo=timezone.utc)
                  if isinstance(value, datetime) and value.tzinfo is None else value)
            for key, value in row.items()}


def _upload_photo(url: str | None, uploaded: dict[str, str]) -> str | None:
    """A /media/<name> photo moved to Vercel Blob; anything else unchanged."""
    prefix = f"{media.MEDIA_URL_PREFIX}/"
    if not url or not url.startswith(prefix):
        return url
    if url in uploaded:
        return uploaded[url]
    name = url[len(prefix):]
    path = ROOT / "data" / "media" / name
    if not path.is_file():
        print(f"  photo missing on disk, left as is: {url}")
        return url
    content_type = {".png": "image/png", ".webp": "image/webp",
                    ".gif": "image/gif"}.get(path.suffix.lower(), "image/jpeg")
    stored = media.save(name, path.read_bytes(), content_type)
    if not stored:
        raise SystemExit(f"Could not upload {name} to Vercel Blob (see the log above).")
    uploaded[url] = stored
    return stored


async def _has_data(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        for name in ("menu_items", "customers", "orders"):
            table = Base.metadata.tables[name]
            if (await conn.execute(select(func.count()).select_from(table))).scalar():
                return True
    return False


async def copy(source: Path, target_url: str, *, replace: bool, upload: bool) -> None:
    src = create_async_engine(f"sqlite+aiosqlite:///{source}", poolclass=NullPool)
    target = driver_url(target_url)
    # asyncpg only: lets the copy also run through a connection pooler.
    options = ({"connect_args": {"statement_cache_size": 0}}
               if target.startswith("postgresql+asyncpg") else {})
    dst = create_async_engine(target, poolclass=NullPool, **options)
    tables = Base.metadata.sorted_tables       # parents before children
    uploaded: dict[str, str] = {}
    try:
        if await _has_data(dst):
            if not replace:
                raise SystemExit("The target database already has data. Run again with "
                                 "--replace to empty it first.")
            async with dst.begin() as conn:
                for table in reversed(tables):
                    await conn.execute(delete(table))
            print("Emptied the target database.")

        async with src.connect() as reader, dst.begin() as writer:
            for table in tables:
                rows = [_aware(dict(r)) for r in (await reader.execute(select(table))).mappings()]
                if upload and table.name == "menu_items":
                    for row in rows:
                        row["image_url"] = _upload_photo(row.get("image_url"), uploaded)
                for start in range(0, len(rows), BATCH):
                    await writer.execute(insert(table), rows[start:start + BATCH])
                print(f"  {table.name:20} {len(rows)}")
        if upload:
            print(f"  photos uploaded      {len(uploaded)}")
    finally:
        await src.dispose()
        await dst.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy a SQLite database into MySQL or Postgres.")
    parser.add_argument("--to", required=True, help="target MySQL or Postgres URL (for Postgres: direct, not pooled)")
    parser.add_argument("--from", dest="source", default=str(ROOT / "shero_local.db"),
                        help="source SQLite file (default shero_local.db)")
    parser.add_argument("--blob-token", default=os.environ.get("BLOB_READ_WRITE_TOKEN", ""),
                        help="Vercel Blob read-write token, to move the dish photos")
    parser.add_argument("--replace", action="store_true",
                        help="empty the target first if it already has data")
    args = parser.parse_args()

    source = Path(args.source)
    if not source.is_file():
        raise SystemExit(f"No SQLite database at {source}")
    if args.blob_token:
        object.__setattr__(settings, "blob_read_write_token", args.blob_token)
    else:
        print("No Blob token: dish photos keep their /media/ paths and will not show on Vercel.")

    print("Creating the tables in the target...")
    _migrate(args.to)
    print("Copying rows...")
    asyncio.run(copy(source, args.to, replace=args.replace, upload=bool(args.blob_token)))
    print("Done.")


if __name__ == "__main__":
    main()
