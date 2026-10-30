"""The /api/nudges endpoints, through the real router."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db.models import AdminUser, InboundMessage
from app.db.session import get_session
from app.integrations.gallabox.sender import use_sender
from tests.services.test_nudges import Phone, left_cart


@pytest.fixture
def app(session, monkeypatch):
    from app.api.routes import nudges

    monkeypatch.setattr(settings, "ops_api_key", "ops-secret")
    monkeypatch.setattr(settings, "admin_session_secret", "test-secret")
    application = FastAPI()
    application.include_router(nudges.router)
    application.dependency_overrides[get_session] = lambda: session
    return application


@pytest.fixture
def api(app):
    client = TestClient(app)
    client.headers["X-Ops-Key"] = "ops-secret"
    return client


CART = {"name": "Cart reminder", "kind": "abandoned_cart",
        "config": {"trigger": {"after_minutes": 45},
                   "message": {"text": "Hi {first_name}, still hungry? {cart_items}",
                               "button_text": "Finish order"}}}


async def test_it_needs_the_ops_key_or_an_admin_sign_in(app, session):
    anonymous = TestClient(app)
    assert anonymous.get("/api/nudges").status_code == 401
    assert anonymous.get("/api/nudges", headers={"X-Ops-Key": "wrong"}).status_code == 401

    from app.admin.auth import SESSION_COOKIE, issue_session
    admin = AdminUser(email="ops@shero.test", name="Ops", password_hash="x", is_active=True)
    session.add(admin)
    await session.flush()
    anonymous.cookies.set(SESSION_COOKIE, issue_session(admin))
    answer = anonymous.get("/api/nudges")
    assert answer.status_code == 200 and answer.json()["enabled"] is False


async def test_settings_and_the_master_switch(api):
    assert api.get("/api/nudges/settings").json()["settings"]["max_per_customer_per_day"] == 1

    bad = api.patch("/api/nudges/settings", json={"timezone": "Mars/Base"})
    assert bad.status_code == 422 and "not a timezone" in bad.json()["detail"]

    saved = api.patch("/api/nudges/settings", json={"dry_run": True, "min_hours_between": 48})
    assert saved.json()["settings"]["dry_run"] is True and saved.json()["enabled"] is False

    off = api.post("/api/nudges/run")
    assert off.status_code == 409 and "switched off" in off.json()["detail"]

    assert api.post("/api/nudges/enable").json() == {"enabled": True}
    assert api.post("/api/nudges/run").status_code == 200
    assert api.post("/api/nudges/disable").json() == {"enabled": False}
    assert api.get("/api/nudges").json()["enabled"] is False


async def test_a_campaigns_life(api):
    wrong = api.post("/api/nudges/campaigns", json={**CART, "config": {"trigger": {"after_minuts": 5}}})
    assert wrong.status_code == 422 and "is not an option" in wrong.json()["detail"]
    assert api.post("/api/nudges/campaigns", json={"kind": "abandoned_cart"}).status_code == 422

    created = api.post("/api/nudges/campaigns", json=CART)
    assert created.status_code == 201
    campaign = created.json()
    assert campaign["is_active"] is False
    assert campaign["config"]["trigger"]["after_minutes"] == 45
    assert campaign["config"]["message"]["button_text"] == "Finish order"
    url = f"/api/nudges/campaigns/{campaign['id']}"

    changed = api.patch(url, json={"config": {"limits": {"cooldown_days": 1}}})
    assert changed.json()["config"]["limits"]["cooldown_days"] == 1
    assert changed.json()["config"]["trigger"]["after_minutes"] == 45     # untouched

    assert api.post(f"{url}/run").status_code == 409                      # paused
    assert api.post(f"{url}/activate").json()["is_active"] is True
    assert api.post(f"{url}/pause").json()["is_active"] is False

    copy = api.post(f"{url}/duplicate").json()
    assert copy["name"] == "Copy of Cart reminder" and copy["is_active"] is False

    detail = api.get(url).json()
    assert detail["stats_30_days"]["totals"]["sent"] == 0
    assert len(api.get("/api/nudges/campaigns").json()["campaigns"]) == 2
    assert api.delete(url).json() == {"deleted": True}
    assert api.get(url).status_code == 404
    assert api.get("/api/nudges/campaigns/not-an-id").status_code == 404


async def test_preview_run_log_and_customer_view(api, session, customer):
    api.patch("/api/nudges/settings", json={"send_window": {"start": "00:00", "end": "23:59"}})
    campaign = api.post("/api/nudges/campaigns", json={**CART, "is_active": True}).json()
    await left_cart(session, customer)
    session.add(InboundMessage(provider_message_id="in-1", whatsapp_number=customer.whatsapp_number,
                               payload={}, created_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    await session.flush()

    preview = api.post(f"/api/nudges/campaigns/{campaign['id']}/preview").json()
    assert preview["totals"]["would_send"] == 1
    assert "Hi Asha, still hungry? 2 × Drumstick Sambar" in preview["outcomes"][0]["message"]

    why = api.get(f"/api/nudges/customers/{customer.whatsapp_number}").json()
    assert why["whatsapp_window_open"] is True
    assert why["campaigns"][0]["status"] == "would_send"

    phone = Phone()
    api.post("/api/nudges/enable")
    with use_sender(phone):
        ran = api.post("/api/nudges/run").json()
    assert ran["totals"]["sent"] == 1 and len(phone.sent) == 1

    log = api.get("/api/nudges/sends", params={"campaign_id": campaign["id"]}).json()
    assert log["total"] == 1 and log["sends"][0]["status"] == "sent"
    assert api.get("/api/nudges/sends", params={"status": "nope"}).status_code == 422
    assert api.get("/api/nudges/stats", params={"days": 7}).json()["totals"]["sent"] == 1

    assert api.post(f"/api/nudges/customers/{customer.whatsapp_number}/opt-out").json() == \
        {"opted_out": True, "changed": True}
    why = api.get(f"/api/nudges/customers/{customer.whatsapp_number}").json()
    assert why["opted_out_at"] is not None
    assert api.post(f"/api/nudges/customers/{customer.whatsapp_number}/opt-in").json()["changed"]
    assert api.get("/api/nudges/customers/19999999999").status_code == 404


async def test_options_feed_the_editors_pickers(api, session, outlet, menu):
    from decimal import Decimal
    from app.db.models import DiscountType, Placement, Promotion
    session.add(Promotion(title="Come back", code="COMEBACK", discount_type=DiscountType.FLAT,
                          discount_value=Decimal("5"), placement=Placement.HIDDEN, is_active=True))
    await session.flush()

    options = api.get("/api/nudges/options").json()

    assert options["coupons"][0] | {"ends_on": None} == {
        "code": "COMEBACK", "title": "Come back", "headline": "$5 off", "placement": "hidden",
        "status": "live", "ends_on": None}
    assert options["kitchens"][0]["code"] == "EDISON"
    assert "Drumstick Sambar" in options["dishes"] and "Chettinad" in options["cuisines"]


async def test_the_page_is_served_and_in_the_sidebar(session, monkeypatch):
    from app.admin import router as admin_router
    from app.admin.deps import require_admin

    monkeypatch.setattr(settings, "admin_session_secret", "test-secret")
    application = FastAPI()
    application.include_router(admin_router)
    application.dependency_overrides[get_session] = lambda: session
    application.dependency_overrides[require_admin] = lambda: AdminUser(
        email="ops@shero.test", name="Ops", password_hash="x", is_active=True)
    client = TestClient(application)

    page = client.get("/admin/nudges")
    assert page.status_code == 200
    assert 'id="switch-card"' in page.text and "/api/nudges" in page.text
    # The script reached the browser whole: Jinja left WhatsApp's {{1}} alone.
    assert "{{${i + 1}}}" in page.text
    assert 'data-tab="messages"' in page.text and "Follow-up journey" in page.text
    # url_for renders absolute links, so match the path's end, not a whole href.
    offers = client.get("/admin/promotions").text
    assert '/admin/nudges"' in offers


async def test_the_reference_lists_every_option(api):
    reference = api.get("/api/nudges/reference").json()
    assert set(reference["kinds"]) == {"abandoned_cart", "abandoned_checkout", "repeat_purchase",
                                       "never_ordered", "targeted_offer", "follow_up"}
    journey = reference["kinds"]["follow_up"]["defaults"]["journey"]
    assert journey["length_days"] == 183 and journey["pool"] == "SMO"
    assert "journey" not in reference["kinds"]["abandoned_cart"]["defaults"]
    assert "cart_items" in reference["placeholders"]
    assert "ordered_dishes" in reference["audience_filters"]
    assert reference["kinds"]["repeat_purchase"]["trigger_options"] == [
        "days_since_last_order", "max_days_since_last_order"]


# --- the follow-up message library ---------------------------------------------------------------
async def test_a_library_messages_life(api):
    bad = api.post("/api/nudges/messages", json={"code": "M1-01", "title": "x", "body": "{nope}"})
    assert bad.status_code == 422 and "not a placeholder" in bad.json()["detail"]

    made = api.post("/api/nudges/messages", json={
        "code": "m1-01", "month": 1, "title": "Rice is sorted", "wa_template": "smo_m1_01",
        "body": "Rice is sorted? Send ORDER FOOD on WhatsApp."})
    assert made.status_code == 201
    message = made.json()
    assert message["code"] == "M1-01" and message["wa_approved"] is True
    url = f"/api/nudges/messages/{message['id']}"

    assert api.patch(url, json={"buckets": ["later"], "is_active": False}).json()["buckets"] == ["later"]
    copy = api.post(f"{url}/duplicate").json()
    assert copy["code"] == "M1-01-COPY"
    listed = api.get("/api/nudges/messages").json()
    assert [m["code"] for m in listed["messages"]] == ["M1-01", "M1-01-COPY"]
    assert listed["pools"] == ["SMO"] and listed["columns"][0]["heading"] == "ID"
    assert api.delete(url).json()["deleted"] is True and api.get(url).status_code == 404


async def test_importing_and_exporting_files(api):
    sheet = ("ID,Month,Title,Message,Groups\n"
             "M1-01,1,Rice is sorted,Rice is sorted? Send ORDER FOOD.,\n"
             "M1-02,1,Same rice,Same rice. Different curry. ORDER FOOD,Later\n"
             "M1-03,1,,No title here,\n")
    upload = {"file": ("plan.csv", sheet.encode(), "text/csv")}
    check = api.post("/api/nudges/messages/import", files=upload).json()
    assert check["committed"] is False and check["counts"]["create"] == 2 and check["counts"]["error"] == 1
    assert api.get("/api/nudges/messages").json()["messages"] == []

    done = api.post("/api/nudges/messages/import", files=upload, data={"commit": "true"}).json()
    assert done["committed"] and len(api.get("/api/nudges/messages").json()["messages"]) == 2

    excel = api.get("/api/nudges/messages/export", params={"format": "xlsx"})
    assert excel.status_code == 200 and excel.content[:2] == b"PK"
    assert "attachment" in excel.headers["content-disposition"]
    exported = api.get("/api/nudges/messages/export", params={"format": "json"}).json()
    assert [m["id"] for m in exported["messages"]] == ["M1-01", "M1-02"]
    assert exported["messages"][1]["groups"] == ["Later"]
    again = api.post("/api/nudges/messages/import",
                     files={"file": ("back.xlsx", excel.content)}).json()
    assert again["counts"]["unchanged"] == 2

    assert api.get("/api/nudges/messages/sample", params={"format": "csv"}).status_code == 200
    assert api.get("/api/nudges/messages/export", params={"format": "pdf"}).status_code == 422
    broken = api.post("/api/nudges/messages/import", files={"file": ("x.json", b"{oops")})
    assert broken.status_code == 422 and "not valid JSON" in broken.json()["detail"]


async def test_a_follow_up_journey_from_the_api(api, session):
    from app.db.models import Customer

    api.patch("/api/nudges/settings", json={"send_window": {"start": "00:00", "end": "23:59"}})
    api.post("/api/nudges/messages", json={"code": "M1-01", "month": 1, "title": "Rice",
                                           "wa_template": "smo_m1_01", "body": "Rice? ORDER FOOD"})
    campaign = api.post("/api/nudges/campaigns", json={
        "name": "SMO follow-up", "kind": "follow_up", "is_active": True,
        "config": {"journey": {"pause_after_chat_hours": 0}}}).json()
    assert campaign["config"]["journey"]["no_response"]["steps"][0] == {"every_days": 3.0, "times": 3}
    session.add(Customer(whatsapp_number="447700900123", name="Asha",
                         created_at=datetime.now(timezone.utc) - timedelta(days=3)))
    await session.flush()

    preview = api.post(f"/api/nudges/campaigns/{campaign['id']}/preview").json()
    assert preview["outcomes"][0]["detail"] == "M1-01 · month 1 · No response"

    api.post("/api/nudges/enable")
    phone = Phone()
    with use_sender(phone):
        assert api.post("/api/nudges/run").json()["totals"]["sent"] == 1
    assert phone.sent[0]["name"] == "smo_m1_01"

    summary = api.get("/api/nudges/journeys/summary").json()["campaigns"][campaign["id"]]
    assert summary["by_bucket"] == {"no_response": 1}
    who = api.get("/api/nudges/customers/447700900123").json()
    journey = who["journeys"][0]
    assert (journey["attempts"], journey["last_message_code"], journey["stage"]) == (1, "M1-01", 1)

    moved = api.post(f"/api/nudges/journeys/{journey['id']}", json={"action": "move", "bucket": "later"})
    assert moved.json()["bucket_label"] == "Later"
    assert api.post(f"/api/nudges/journeys/{journey['id']}", json={"action": "fly"}).status_code == 422
    assert api.post("/api/nudges/journeys/nope", json={"action": "end"}).status_code == 404
    assert api.get("/api/nudges/stats").json()["messages"][0]["code"] == "M1-01"
