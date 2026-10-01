"""/cron/tick and /cron/daily: the timed jobs, called by Vercel Cron.

Vercel sends "Authorization: Bearer <CRON_SECRET>" on every cron call. Any
other caller is refused, and with CRON_SECRET unset nobody is let in, so the
jobs can never be triggered by a stranger. Any outside scheduler that can
send that header (cron-job.org, GitHub Actions) works the same way.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.config import is_unset, settings
from app.core.logging import get_logger
from app.workers import cron

log = get_logger(__name__)
router = APIRouter(prefix="/cron", tags=["cron"], include_in_schema=False)


def _refusal(request: Request) -> JSONResponse | None:
    expected = settings.cron_secret.strip()
    if is_unset(expected):
        log.error("cron_refused", reason="CRON_SECRET is not set")
        return JSONResponse({"error": "CRON_SECRET is not set"}, status_code=503)
    given = request.headers.get("authorization", "")
    if not secrets.compare_digest(given.encode(), f"Bearer {expected}".encode()):
        log.warning("cron_refused", reason="bad or missing secret")
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


@router.get("/tick")
async def tick(request: Request):
    """Every minute: the payment jobs, and every fifth minute the rest."""
    refused = _refusal(request)
    if refused:
        return refused
    return {"ok": True, "ran": await cron.tick()}


@router.get("/daily")
async def daily(request: Request):
    """Once a day: top up delivery slots."""
    refused = _refusal(request)
    if refused:
        return refused
    return {"ok": True, "ran": await cron.tick(daily=True)}
