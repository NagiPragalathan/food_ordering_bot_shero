"""Build the clean starter database that is committed to the repo.

    python -m scripts.make_seed_db [source.db]

Copies the local SQLite database (default shero_local.db) to
deploy/seed/shero.db and keeps only what a new server needs to start: the
menu (cuisines, categories, dishes) and the kitchens. Everything personal or
secret is removed - customers, chats, addresses, orders, saved settings and
keys, and admin logins - because the repo is public. Delivery slots are
dropped too; the bot regenerates them from the kitchens' opening hours.

The dish photos the menu uses are copied to deploy/seed/media/.

On the server the seed is copied into data/ once (see
docs/aws-hosting-guide.pdf); after that data/shero.db is the live database
and is never overwritten by a git pull.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "deploy" / "seed"
MEDIA_SRC = ROOT / "data" / "media"

# Kept as they are. Every other table is emptied.
KEEP = {"alembic_version", "cuisines", "categories", "menu_items", "outlets"}


def build(source: Path) -> Path:
    if not source.exists():
        raise SystemExit(f"No database at {source}")
    SEED_DIR.mkdir(parents=True, exist_ok=True)
    target = SEED_DIR / "shero.db"
    target.unlink(missing_ok=True)

    # The backup API gives a consistent copy even while the bot is running.
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)

    db = sqlite3.connect(target)
    try:
        tables = [t for (t,) in db.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
        db.execute("pragma foreign_keys = off")
        for table in tables:
            if table not in KEEP:
                db.execute(f'delete from "{table}"')
        db.commit()
        db.execute("vacuum")          # really drop the deleted rows from the file
        problem = db.execute("pragma integrity_check").fetchone()[0]
        if problem != "ok":
            raise SystemExit(f"Seed database failed its integrity check: {problem}")
        counts = {t: db.execute(f'select count(*) from "{t}"').fetchone()[0]
                  for t in sorted(KEEP & set(tables))}
        images = [row[0] for row in db.execute(
            "select image_url from menu_items where image_url like '/media/%'")]
    finally:
        db.close()

    copied = _copy_media(images)
    for table, count in counts.items():
        print(f"  {table:16} {count}")
    print(f"  photos           {copied}")
    print(f"Wrote {target.relative_to(ROOT)} ({target.stat().st_size // 1024} KB)")
    return target


def _copy_media(urls: list[str]) -> int:
    """Copy the photos the menu points at (thumbnails are rebuilt on demand)."""
    out = SEED_DIR / "media"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    copied = 0
    for url in urls:
        name = url.removeprefix("/media/").split("?")[0]
        source = MEDIA_SRC / name
        if source.is_file() and "/" not in name:
            shutil.copy2(source, out / name)
            copied += 1
        else:
            print(f"  missing photo: {url}")
    return copied


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "shero_local.db")
