"""Shero US runs exactly as it did before DST India shared the server.

A broken or blank DST setting, DST India's own menu rules and its scheduled
jobs must never change what Shero US does or stores (app/core/tenants.py).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core import config, tenants
from app.db.models import MenuItem
from app.services import erp_import
from app.services.menu import on_sale


@pytest.fixture
def fresh_dst(monkeypatch):
    """DST India's settings rebuilt from the environment in this test, and
    the live ones put back afterwards."""
    saved = config._instances.pop(tenants.DST.key, None)
    monkeypatch.setattr(tenants, "_broken", {})
    yield
    config._instances.pop(tenants.DST.key, None)
    if saved is not None:
        config._instances[tenants.DST.key] = saved


def _admin(session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.admin import router
    from app.admin.deps import require_admin
    from app.db.models import AdminUser
    from app.db.session import get_session

    app = FastAPI()
    app.include_router(router)
    app.add_middleware(tenants.TenantPathMiddleware)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_admin] = lambda: AdminUser(
        email="ops@shero.test", name="Ops", password_hash="x", is_active=True)
    return TestClient(app)


async def test_a_mistyped_dst_setting_leaves_shero_us_running(fresh_dst, monkeypatch, session):
    from app.integrations.gallabox import channel
    from app.schemas.inbound import parse_inbound
    from app.workers import jobs

    monkeypatch.setenv("DST_TAX_PERCENT", "5%")          # not a number
    with pytest.raises(ValidationError):
        config.build_settings(tenants.DST)

    # DST India is left out, by the variable's name only; Shero US carries on.
    assert tenants.enabled() == [tenants.US]
    assert tenants._broken["dst"] == "invalid: DST_TAX_PERCENT"

    # A WhatsApp message on the US number still reaches Shero US, and another
    # team's number is still refused, as before...
    us = config.settings_for(tenants.US)
    monkeypatch.setattr(us, "gallabox_channel_id", "us-channel")
    monkeypatch.setattr(us, "whatsapp_business_number", "14438011011")

    def message(channel_id, number):
        return parse_inbound({"accountId": "acc", "channelType": "whatsapp", "id": "m1",
                              "channelId": channel_id, "channelNumber": number,
                              "contact": {"name": "Asha"},
                              "whatsapp": {"from": "17325550142", "type": "text",
                                           "text": {"body": "hi"}}})

    assert channel.tenant_for(message("us-channel", "14438011011")) == tenants.US
    assert channel.tenant_for(message("someone-else", "15550000000")) is None
    # ...its jobs run (only for Shero US)...
    seen = []

    async def job():
        seen.append(tenants.current().key)
        return 1

    assert await jobs.everywhere(job) == 1 and seen == ["us"]
    # ...and its admin pages load, without a DST India tab.
    page = _admin(session).get("/admin/")
    assert page.status_code == 200 and "/dst/admin/" not in page.text


def test_a_blank_dst_database_line_means_the_automatic_file(fresh_dst, monkeypatch):
    """`DST_DATABASE_URL=` as .env.example has it is "not set", not "none":
    the migration and DST India both get the file beside Shero US's."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./data/shero.db")
    monkeypatch.setenv("DST_DATABASE_URL", "")
    built = config.build_settings(tenants.DST)
    assert built.database_url == "sqlite+aiosqlite:///./data/shero_dst.db"
    assert config.build_settings(tenants.US).database_url == "sqlite+aiosqlite:///./data/shero.db"


async def test_shero_us_still_sells_a_dish_at_zero_dst_india_does_not(session, menu):
    dish = (await session.execute(select(MenuItem).limit(1))).scalar_one()
    dish.price, dish.is_available = Decimal("0.00"), True
    await session.flush()

    async def sold() -> int:
        return await session.scalar(select(func.count(MenuItem.id)).where(
            on_sale(), MenuItem.id == dish.id))

    assert await sold() == 1                       # Shero US: as it always has
    with tenants.use(tenants.DST):
        assert await sold() == 0                   # DST India: priced only


async def test_the_photo_job_touches_only_erp_photos(session, menu, monkeypatch):
    dishes = list((await session.execute(select(MenuItem).limit(2))).scalars())
    dishes[0].image_url = "https://cdn.example.com/menu/dosa.jpg"     # a US photo link
    dishes[1].image_url = "https://sherohomeerp-shero.s3.ap-south-1.amazonaws.com/items/x.jpg"
    await session.flush()
    fetched = []

    async def fetch_many(urls):
        fetched.extend(urls)
        return {url: "/media/x.jpg" for url in urls}

    monkeypatch.setattr(erp_import.media, "fetch_many", fetch_many)
    assert await erp_import.photos_waiting(session) == 1
    assert await erp_import.store_remote_photos(session) == 1
    assert fetched == ["https://sherohomeerp-shero.s3.ap-south-1.amazonaws.com/items/x.jpg"]
    assert dishes[0].image_url == "https://cdn.example.com/menu/dosa.jpg"


async def test_each_business_has_its_own_scheduled_jobs():
    """Shero US's jobs keep their names; DST India's are separate jobs, so a
    slow or failing DST run never delays or fails a Shero US one."""
    from app.workers import scheduler

    running = scheduler.start_scheduler()
    try:
        ids = {job.id for job in running.get_jobs()}
    finally:
        scheduler.shutdown_scheduler()
    us = {"payment_reminders", "payment_expiry", "feedback_requests", "courier_dispatch",
          "kitchen_alerts", "nudges", "menu_photos", "slot_generation", "settings_refresh"}
    others = {f"{t.key}_{name}" for t in tenants.enabled() if not t.is_default for name in us}
    # DST India's own CRM has one job more; Shero US's list is unchanged.
    own = {"dst_crm_outbox"} if tenants.DST in tenants.enabled() else set()
    # Shero Food Training (app/sft/) is Shero US's alone: no dst_ copies.
    sft = {"sft_nudges", "sft_crm"}
    assert ids == us | others | own | sft
