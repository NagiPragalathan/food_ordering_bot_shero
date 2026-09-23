"""Async engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

def _engine_options(url: str) -> dict:
    """Pool settings for the configured database.

    SQLite - used by the tests and by a local run with no Postgres installed -
    is served by a pool class that takes none of the sizing arguments below,
    so passing them raises `TypeError` at import time rather than failing
    later. Keep the two dialects apart explicitly.
    """
    if url.startswith("sqlite"):
        return {}
    return {
        "pool_pre_ping": True,   # webhooks arrive in bursts after idle periods
        "pool_size": 10,
        "max_overflow": 20,
    }


engine = create_async_engine(
    settings.database_url,
    echo=False,
    **_engine_options(settings.database_url),
)

SessionFactory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


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
