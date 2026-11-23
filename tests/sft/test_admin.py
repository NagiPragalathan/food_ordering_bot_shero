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


TABS = ["overview", "leads", "payments", "customers", "nudges", "offers", "replies", "journey",
        "chat", "settings"]


@pytest.mark.parametrize("tab", TABS)
def test_every_tab_opens(admin, tab):
    page = admin.get(f"/admin/sft?tab={tab}")
    assert page.status_code == 200, page.text[:500]
    assert "Shero Food Training" in page.text and "data-sft-sidebar" in page.text
    assert f'href="http://testserver/admin/sft?tab={tab}" aria-current="page"' in page.text


def test_the_old_templates_tab_opens_nudges(admin):
    page = admin.get("/admin/sft?tab=templates").text
    assert 'href="http://testserver/admin/sft?tab=nudges" aria-current="page"' in page
    assert "Submit the missing ones to WhatsApp" in page


@pytest.mark.parametrize("path", ["/admin/sft", "/admin/sft?tab=settings", "lead"])
async def test_sft_has_its_own_menu_not_the_food_dashboards(admin, session, cfg, rec, path):
    if path == "lead":
        await say(session, cfg, "SFT")
        path = f"/admin/sft/leads/{(await leads.get(session, NUMBER)).id}"
    page = admin.get(path).text
    for key in TABS:
        assert f"/admin/sft?tab={key}" in page, key
    for food in ("Kitchens", "Uber queue", "Payouts", "Loyalty", "DST India",
                 "data-business-tabs", "admin/menu", "admin/bot-replies", "admin/orders"):
        assert food not in page, food


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


async def test_who_sft_answers_is_saved_on_bot_replies(admin, session):
    saved = admin.post("/admin/sft/replies", data={
        "mode": "test", "test_numbers": "+1 555 123 0001", "keywords": "sft, food course",
        "has_ask_name": "1"})
    assert saved.status_code == 303
    cfg = await config.load(session, fresh=True)
    assert cfg.mode == "test" and cfg.test_numbers == ["15551230001"]
    assert cfg.keywords == ["sft", "food course"] and not cfg.ask_name
    bad = admin.post("/admin/sft/replies", data={"keywords": ""})
    assert "keyword" in bad.cookies.get("shero_flash", "")
    assert (await config.load(session, fresh=True)).keywords == ["sft", "food course"]


async def test_settings_are_saved_and_checked(admin, session):
    saved = admin.post("/admin/sft/settings", data={
        "course_url": "https://learn.example/sft", "has_zoho_sync": "1", "has_zoho_tasks": "1",
        "zoho_sync": "on"})
    assert saved.status_code == 303
    cfg = await config.load(session, fresh=True)
    assert cfg.course_url == "https://learn.example/sft" and cfg.zoho_sync and not cfg.zoho_tasks
    insecure = admin.post("/admin/sft/settings", data={"course_url": "http://x.example"})
    assert insecure.status_code == 303
    assert (await config.load(session, fresh=True)).course_url == "https://learn.example/sft"


async def test_nudges_are_saved_and_checked(admin, session):
    saved = admin.post("/admin/sft/nudges", data={
        "nudges_on": "on", "followup_1_hours": "24", "followup_1_on": "on",
        "followup_2_hours": "72", "followup_3_hours": "144", "followup_3_on": "on",
        "payment_reminder_minutes": "90", "payment_reminder_on": "on", "nurture_days": "14, 30",
        "access_check_hours": "12", "attention_brochure_sent": "12", "send_from_hour": "8",
        "send_until_hour": "21", "timezone": "America/Chicago"})
    assert saved.status_code == 303
    cfg = await config.load(session, fresh=True)
    assert cfg.followup_hours == [24, 72, 144] and cfg.nudges_on
    # Follow-up 2, the check-in and the course check were left unticked: off.
    assert set(cfg.nudges_off) == {"followup_2", "nurture", "course_check"}
    assert cfg.payment_reminder_minutes == 90 and cfg.nurture_days == [14, 30]
    assert cfg.attention_hours["brochure_sent"] == 12 and cfg.access_check_hours == 12
    assert (cfg.send_from_hour, cfg.send_until_hour, cfg.timezone) == (8, 21, "America/Chicago")

    bad = admin.post("/admin/sft/nudges", data={"followup_1_hours": "72", "followup_2_hours": "24"})
    assert "shortest to longest" in bad.cookies.get("shero_flash", "")
    assert (await config.load(session, fresh=True)).followup_hours == [24, 72, 144]
    none = admin.post("/admin/sft/nudges", data={"nudges_on": "on"})
    assert "at least one follow-up" in none.cookies.get("shero_flash", "")
