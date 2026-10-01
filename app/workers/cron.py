"""The scheduler's timetable, for hosts that call in from outside.

On a long-lived server APScheduler runs the jobs (scheduler.py). On Vercel
nothing runs between requests, so Vercel Cron calls /cron/tick every minute
and /cron/daily once a day (api/routes/cron.py), and this module decides
which jobs are due on each call, keeping the same rhythm as the scheduler:

    every minute     payment reminders, payment expiry
    every 5 minutes  feedback requests, courier booking, kitchen alerts
    daily            delivery slot top-up

A minute Vercel skips simply delays the 5-minute jobs to the next multiple
of five; every job picks up whatever has become due since its last run.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.logging import get_logger
from app.workers import jobs

log = get_logger(__name__)

EVERY_MINUTE = ("payment_reminders", "payment_expiry")
EVERY_5_MINUTES = ("feedback_requests", "courier_dispatch", "kitchen_alerts")
DAILY = ("slot_generation",)


def due(now: datetime, *, daily: bool = False) -> tuple[str, ...]:
    """The jobs to run on a call made at `now`."""
    if daily:
        return DAILY
    return EVERY_MINUTE + (EVERY_5_MINUTES if now.minute % 5 == 0 else ())


async def run(names: tuple[str, ...]) -> dict[str, object]:
    """Run each job; one failing does not stop the others.

    Returns {job: count of items handled, or "error: ..."}.
    """
    results: dict[str, object] = {}
    for name in names:
        try:
            results[name] = await jobs.ALL_JOBS[name]()
        except Exception as exc:  # noqa: BLE001 - report it and run the next job
            log.error("cron_job_failed", job=name, error=str(exc))
            results[name] = f"error: {exc}"
    return results


async def tick(now: datetime | None = None, *, daily: bool = False) -> dict[str, object]:
    now = now or datetime.now(timezone.utc)
    results = await run(due(now, daily=daily))
    log.info("cron_tick", daily=daily, results=results)
    return results
