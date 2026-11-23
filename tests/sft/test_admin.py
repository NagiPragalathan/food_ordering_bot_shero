"""SFT's admin page (/admin/sft): every tab, the test chat, the team's actions."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin import router
from app.admin.deps import require_admin
from app.core import tenants
from app.db.models import AdminUser
from app.db.session import get_session
from app.sft import config, leads
from app.sft import content as c
from tests.sft.conftest import NUMBER, say


@pytest.fixture
def admin(session):
    app = FastAPI()
    app.include_router(router)
    app.add_middleware(tenants.TenantPathMiddleware)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_admin] = lambda: AdminUser(
        email="ops@shero.test", name="Ops", password_hash="x", is_active=True)
    return TestClient(app, follow_redirects=False)


@pytest.mark.parametrize("tab", ["overview", "leads", "chat", "journey", "settings", "templates"])
def test_every_tab_opens(admin, tab):
    page = admin.get(f"/admin/sft?tab={tab}")
    assert page.status_code == 200, page.text[:500]
    assert "Shero Food Training (SFT)" in page.text


def test_it_is_not_in_the_dashboards_menu(admin):
    assert "/admin/sft" not in admin.get("/admin/").text


def test_there_is_no_dst_copy(admin):
    assert admin.get("/dst/admin/sft").status_code == 404


def test_off_by_default_says_so(admin):
    assert "SFT is <b>off</b>" in admin.get("/admin/sft").text


def test_the_journey_shows_the_new_messages(admin):
    page = admin.get("/admin/sft?tab=journey").text
    assert "Yes, enroll me" in page and "Not right now" in page
    assert "transferring you to a member of our team" in page
    assert "Start food business" in page and "Course details</span>" not in page


async def test_the_test_chat_walks_the_journey(admin, session):
    number = "15550000077"
    admin.post("/admin/sft/chat", data={"number": number, "text": "SFT"})
    admin.post("/admin/sft/chat", data={"number": number, "text": "Priya"})
    page = admin.get(f"/admin/sft?tab=chat&number={number}").text
    assert "Hi Priya!" in page and "Start food business" in page
    admin.post("/admin/sft/chat", data={"number": number, "button": c.B_BUSINESS,
                                        "title": "Start food business"})
    lead = await leads.get(session, number)
    assert lead.source == "Test chat" and lead.stage == c.INTERESTED and not lead.crm_dirty
    # Time travel: the follow-up job a day later.
    done = admin.post("/admin/sft/chat", data={"number": number, "action": "wait", "hours": "30"})
    assert done.status_code == 303
    admin.post("/admin/sft/chat", data={"number": number, "action": "reset"})
    assert await leads.get(session, number) is None


async def test_the_test_chat_never_touches_a_real_lead(admin, session, cfg, rec):
    await say(session, cfg, "SFT")
    answer = admin.post("/admin/sft/chat", data={"number": NUMBER, "action": "reset"})
    assert await leads.get(session, NUMBER) is not None
    assert answer.status_code == 303


async def test_the_team_answers_and_hands_back(admin, session, cfg, rec):
    await say(session, cfg, "SFT")
    await say(session, cfg, "Priya")
    await say(session, cfg, "Do you run classes on weekends?")
    lead = await leads.get(session, NUMBER)
    page = admin.get(f"/admin/sft/leads/{lead.id}").text
    assert "Do you run classes on weekends?" in page and "hand back to the bot" in page
    assert "Priya" in admin.get("/admin/sft?tab=overview").text          # in the team's queue

    admin.post(f"/admin/sft/leads/{lead.id}/action", data={"action": "hand_back"})
    assert not lead.with_team and not lead.needs_attention
    rec.clear()
    await say(session, cfg, "SFT")
    assert rec.sent                                                     # the bot answers again


async def test_closing_as_lost_and_nurturing(admin, session, cfg, rec):
    await say(session, cfg, "SFT")
    lead = await leads.get(session, NUMBER)
    admin.post(f"/admin/sft/leads/{lead.id}/action", data={"action": "nurture30"})
    assert lead.stage == c.NURTURE and lead.nurture_until is not None
    admin.post(f"/admin/sft/leads/{lead.id}/action", data={"action": "lost", "reason": "Too far"})
    assert lead.stage == c.LOST and lead.opted_out and lead.lost_reason == "Too far"


async def test_settings_are_saved_and_checked(admin, session):
    saved = admin.post("/admin/sft/settings", data={
        "mode": "test", "test_numbers": "+1 555 123 0001", "keywords": "sft, food course",
        "course_url": "https://learn.example/sft", "followup_hours": "24, 72, 144",
        "attention_brochure_sent": "12", "ask_name": "on", "zoho_sync": "on"})
    assert saved.status_code == 303
    cfg = await config.load(session, fresh=True)
    assert cfg.mode == "test" and cfg.test_numbers == ["15551230001"]
    assert cfg.keywords == ["sft", "food course"] and cfg.followup_hours == [24, 72, 144]
    assert cfg.attention_hours["brochure_sent"] == 12 and not cfg.zoho_tasks

    bad = admin.post("/admin/sft/settings", data={"followup_hours": "72, 24"})
    assert "shortest to longest" in bad.cookies.get("shero_flash", "") or bad.status_code == 303
    assert (await config.load(session, fresh=True)).followup_hours == [24, 72, 144]
    insecure = admin.post("/admin/sft/settings", data={"course_url": "http://x.example"})
    assert insecure.status_code == 303
    assert (await config.load(session, fresh=True)).course_url == "https://learn.example/sft"
