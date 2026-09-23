"""FastAPI application entry point.

    uvicorn app.main:app --reload

Startup logs any missing credentials by name rather than refusing to boot, so
a partially configured environment can still serve /health while the client
finishes handing over access.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.admin import router as admin_router
from app.admin.deps import NotAuthenticated
from app.api.routes import (health, ops, order_web, pay, webhooks_gallabox,
                            webhooks_stripe)
from app.core.config import settings
from app.core.exceptions import IntegrationError, SheroError
from app.core.logging import configure_logging, get_logger
from app.db.session import engine, session_scope
from app.integrations.gallabox.client import gallabox
from app.integrations.meta.catalog import meta_catalog
from app.integrations.uber.direct import uber_direct
from app.integrations.zoho.client import zoho_client
from app.admin.auth import bootstrap_first_user
from app.services.settings_store import apply_overrides
from app.workers.scheduler import shutdown_scheduler, start_scheduler

configure_logging()
log = get_logger(__name__)

DESCRIPTION = """
WhatsApp ordering bot for Shero Home Food.

Meta Ads -> WhatsApp (Gallabox) -> Meta Catalogue -> nearby outlet and slot
-> Uber Direct quote -> Stripe payment -> Zoho CRM.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    missing = settings.missing_credentials()
    if missing:
        log.warning("starting_with_missing_credentials", missing=missing)
    log.info("application_starting", environment=settings.app_env,
             outlet_source=settings.outlet_source,
             slot_source=settings.slot_source)

    # Load admin overrides over the environment, and make sure someone can
    # actually sign in to the dashboard.
    try:
        async with session_scope() as session:
            await apply_overrides(session)
            await bootstrap_first_user(session)
    except Exception as exc:  # noqa: BLE001 - the API must still serve /health
        log.error("admin_startup_failed", error=str(exc))

    if settings.enable_scheduler:
        start_scheduler()
    else:
        log.info("scheduler_disabled_on_this_instance")

    yield

    shutdown_scheduler()
    # Release pooled connections held by the long-lived integration clients.
    for client in (gallabox, zoho_client, meta_catalog, uber_direct):
        await client.aclose()
    await engine.dispose()
    log.info("application_stopped")


app = FastAPI(
    title="Shero WhatsApp Ordering Bot",
    description=DESCRIPTION,
    version="1.0.0",
    lifespan=lifespan,
    # The docs are useful in staging but should not be public in production.
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
)

app.include_router(health.router)
app.include_router(webhooks_gallabox.router)
app.include_router(webhooks_stripe.router)
app.include_router(pay.router)
app.include_router(order_web.router)
app.include_router(ops.router)
app.include_router(admin_router)


@app.exception_handler(NotAuthenticated)
async def not_authenticated_handler(request: Request, _: NotAuthenticated):
    """Send signed-out admins to the login page rather than a bare 401."""
    return RedirectResponse(request.url_for("admin_login_form"), status_code=303)


@app.exception_handler(IntegrationError)
async def integration_error_handler(_: Request, exc: IntegrationError) -> JSONResponse:
    """An upstream failed. 502 so the caller can distinguish it from our bugs."""
    log.error("unhandled_integration_error", service=exc.service, error=str(exc))
    return JSONResponse(
        status_code=502,
        content={"error": "upstream_unavailable", "service": exc.service},
    )


@app.exception_handler(SheroError)
async def shero_error_handler(_: Request, exc: SheroError) -> JSONResponse:
    log.error("unhandled_application_error", error=str(exc))
    return JSONResponse(status_code=500, content={"error": "internal_error"})


@app.get("/", include_in_schema=False)
async def root() -> dict:
    return {
        "service": "shero-whatsapp-ordering-bot",
        "version": app.version,
        "environment": settings.app_env,
    }
