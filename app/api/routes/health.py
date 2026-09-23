"""Liveness and readiness probes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import get_session

log = get_logger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health")
async def liveness() -> dict:
    """Is the process up? Used by the load balancer."""
    return {"status": "ok", "environment": settings.app_env}


@router.get("/health/readiness")
async def readiness(
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Is the service actually able to serve?

    Reports missing credentials by name (never their values) so a
    half-configured deployment is obvious at a glance rather than failing on
    the first real customer.
    """
    checks: dict[str, object] = {}

    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        log.error("readiness_db_failed", error=str(exc))
        checks["database"] = "unavailable"

    missing = settings.missing_credentials()
    checks["credentials"] = "ok" if not missing else "incomplete"
    checks["missing_credentials"] = missing
    checks["outlet_source"] = settings.outlet_source
    checks["slot_source"] = settings.slot_source

    ready = checks["database"] == "ok" and not missing
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {"status": "ready" if ready else "not ready", "checks": checks}
