"""Async engines and session factories: one database per business.

Shero US and DST India never share a table (app/core/tenants.py). A session
opened while serving DST India is a session on DST India's database; the
engine for each is made on first use and kept for the process.

Admin logins are the exception: they live in Shero US's database, and the
admin pages of both businesses check them there (`primary_session_scope`).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (AsyncEngine, AsyncSession, async_sessionmaker,
                                    create_async_engine)
from sqlalchemy.pool import NullPool

from app.core import tenants
from app.core.config import settings, settings_for
from app.core.exceptions import ConfigurationError
from app.db.url import driver_url


def _engine_options(url: str, *, serverless: bool = False) -> dict:
    """Pool settings for the configured database.

    SQLite - used by the tests and by a local run with no Postgres installed -
    is served by a pool class that takes none of the sizing arguments below,
    so passing them raises `TypeError` at import time rather than failing
    later. Keep the two dialects apart explicitly.

    On Vercel no connection is kept between requests (NullPool): a function
    instance may be frozen or dropped at any moment, and a pooled connection
    left open would go stale. Prepared-statement caching is off so the same
    URL also works through a connection pooler (Neon's "-pooler" host).
    """
    if url.startswith("sqlite"):
        return {}
    if serverless:
        return {"poolclass": NullPool,
                "connect_args": {"statement_cache_size": 0}}
    options = {
        "pool_pre_ping": True,   # webhooks arrive in bursts after idle periods
        "pool_size": 10,
        "max_overflow": 20,
    }
    if url.startswith("mysql"):
        # MySQL closes connections idle for longer than its wait_timeout;
        # renew them well before that instead of meeting a dead one.
        options["pool_recycle"] = 1800
    return options


def _make_engine(url: str) -> AsyncEngine:
    return create_async_engine(
        url, echo=False, **_engine_options(url, serverless=settings.is_serverless))


# Shero US's engine, made at import as it always was (scripts and the
# startup checks use it directly).
DATABASE_URL = driver_url(settings_for(tenants.DEFAULT).database_url)
engine = _make_engine(DATABASE_URL)

_engines: dict[str, AsyncEngine] = {tenants.DEFAULT.key: engine}
_factories: dict[str, async_sessionmaker] = {}


def engine_for(tenant: tenants.Tenant) -> AsyncEngine:
    """The engine of `tenant`'s own database."""
    found = _engines.get(tenant.key)
    if found is None:
        url = settings_for(tenant).database_url.strip()
        if not url:
            raise ConfigurationError(
                f"{tenant.name} has no database: set {tenant.env_prefix}DATABASE_URL")
        found = _engines[tenant.key] = _make_engine(driver_url(url))
    return found


def factory_for(tenant: tenants.Tenant) -> async_sessionmaker:
    found = _factories.get(tenant.key)
    if found is None:
        found = _factories[tenant.key] = async_sessionmaker(
            engine_for(tenant), expire_on_commit=False, class_=AsyncSession)
    return found


def SessionFactory() -> AsyncSession:  # noqa: N802 - kept the old name for callers
    """A new session on the current business's database."""
    return factory_for(tenants.current())()


async def dispose_all() -> None:
    for each in _engines.values():
        await each.dispose()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: one session per request, committed on clean exit."""
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Same contract as `get_session`, for background jobs outside a request."""
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_primary_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: a session where the admin logins are."""
    async with primary_session_scope() as session:
        yield session


@asynccontextmanager
async def primary_session_scope() -> AsyncGenerator[AsyncSession, None]:
    """A session on Shero US's database, whichever business is being served:
    where the admin logins are. The business being served stays as it is."""
    async with factory_for(tenants.DEFAULT)() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
