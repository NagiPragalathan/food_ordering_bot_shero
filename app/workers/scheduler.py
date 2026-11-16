"""APScheduler wiring.

Runs inside the API process. That is the right trade-off for a single-service
deployment, but it means **exactly one instance may run the scheduler**: two
would double-send every reminder. Scale the API horizontally with
`ENABLE_SCHEDULER=false` on the extra instances, or split the scheduler into
its own one-replica service.

The one exception is `settings_refresh`, which only reads and is harmless to
run everywhere - but it is registered here with the rest, so a replica with
the scheduler off picks up settings changes on its next restart instead.
"""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.logging import get_logger
from app.workers import jobs

log = get_logger(__name__)

_scheduler: AsyncIOScheduler | None = None


def start_scheduler() -> AsyncIOScheduler:
    """Register the jobs and start the scheduler.

    Every job runs once per business (Shero US, DST India), each in its own
    database, so one scheduler serves both."""
    from app.core import tenants
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    scheduler = AsyncIOScheduler(timezone="UTC")

    def add(job, trigger, *, id: str, **options) -> None:
        """One scheduler job per business. Shero US keeps the job's own id,
        exactly as before DST India existed; another business gets
        `<key>_<id>` (dst_nudges). Separate jobs, so a slow or failing DST
        India run never delays, skips or fails a Shero US one."""
        for tenant in tenants.enabled():
            job_id = id if tenant.is_default else f"{tenant.key}_{id}"
            scheduler.add_job(jobs.in_business(tenant, job), trigger, id=job_id, **options)

    # Payment chasing runs on a tight loop: a minute of drift on a 15-minute
    # reminder or a 30-minute expiry is acceptable, ten minutes is not.
    add(
        jobs.send_payment_reminders, IntervalTrigger(minutes=1),
        id="payment_reminders", max_instances=1, coalesce=True,
        misfire_grace_time=120,
    )
    add(
        jobs.expire_payment_links, IntervalTrigger(minutes=1),
        id="payment_expiry", max_instances=1, coalesce=True,
        misfire_grace_time=120,
    )
    add(
        jobs.send_feedback_requests, IntervalTrigger(minutes=5),
        id="feedback_requests", max_instances=1, coalesce=True,
        misfire_grace_time=300,
    )
    # Couriers are booked 2 hours ahead, so a few minutes' drift is harmless.
    add(
        jobs.dispatch_couriers, IntervalTrigger(minutes=5),
        id="courier_dispatch", max_instances=1, coalesce=True,
        misfire_grace_time=300,
    )
    # Kitchen alerts go out at KITCHEN_ALERT_HOUR on the delivery day.
    add(
        jobs.send_kitchen_alerts, IntervalTrigger(minutes=5),
        id="kitchen_alerts", max_instances=1, coalesce=True,
        misfire_grace_time=300,
    )
    # Nudges by stage. Off until switched on on the Nudges page.
    add(
        jobs.send_nudges, IntervalTrigger(minutes=5),
        id="nudges", max_instances=1, coalesce=True,
        misfire_grace_time=300,
    )
    # Dish photos still linked from the ERP, copied to our store a batch at a time.
    add(
        jobs.store_menu_photos, IntervalTrigger(minutes=5),
        id="menu_photos", max_instances=1, coalesce=True,
        misfire_grace_time=300,
    )
    # Slots only need topping up occasionally; 03:00 UTC is outside service
    # hours for the US east-coast outlets in the spec.
    add(
        jobs.generate_upcoming_slots, CronTrigger(hour=3, minute=0),
        id="slot_generation", max_instances=1, coalesce=True,
    )

    # Settings saved on another instance are picked up here. This one is
    # safe to run on every replica - it only reads.
    add(
        jobs.refresh_settings, IntervalTrigger(minutes=1),
        id="settings_refresh", max_instances=1, coalesce=True,
        misfire_grace_time=120,
    )

    scheduler.start()
    _scheduler = scheduler
    log.info("scheduler_started",
             jobs=[job.id for job in scheduler.get_jobs()])
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("scheduler_stopped")
    _scheduler = None
