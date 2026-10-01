"""Alembic environment.

Reads DATABASE_URL from application settings so migrations and the app can
never drift onto different databases. Supports async online migrations
(asyncpg) and offline `--sql` rendering.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import settings
from app.db.url import driver_url

# Importing the models package registers every table on Base.metadata.
from app.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The same driver-form URL the app uses (hosted Postgres URLs are rewritten).
DATABASE_URL = driver_url(settings.database_url)
# "%" is configparser syntax; an encoded password (%40) must be escaped.
config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))

target_metadata = Base.metadata


BACKEND = make_url(DATABASE_URL).get_backend_name()


def _configure(connection: Connection | None = None, **kwargs) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Catch column type changes, not just added/dropped columns.
        compare_type=True,
        compare_server_default=True,
        # SQLite cannot ALTER a column in place; batch mode rebuilds the table
        # instead. Postgres does not need it, and turning it on there would
        # only obscure the emitted SQL.
        render_as_batch=(BACKEND == "sqlite"),
        **kwargs,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (`alembic upgrade head --sql`)."""
    _configure(
        url=DATABASE_URL,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # No live connection, so the dialect must be stated explicitly.
        dialect_name=BACKEND,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
