"""The Nudges page: the on/off switch and send hours, the targeting rules, the
No / Later / No response sequences, the messages, and who got what."""
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin import router
from app.admin.deps import require_admin
from app.db.models import AdminUser, LeadStage, NudgeBucket, NudgeLead, NudgeSend
from app.db.session import get_session
from app.services.nudges import config, messages
from app.services.nudges.config import STAGES

CART = STAGES.index(LeadStage.CART_CREATED.value)
TEXT = "Hi {name}, your Shero meal is still in your cart. Would you like it today?"


@pytest.fixture
def admin(session, monkeypatch):
    submitted = []

    async def submit(message):
        submitted.append(message.name)
        return "pending"

    async def statuses(*, fresh=False):
        return {name: "approved" for name in submitted}

    monkeypatch.setattr(messages, "submit", submit)
    monkeypatch.setattr(messages, "statuses", statuses)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_admin] = lambda: AdminUser(
        email="ops@shero.test", name="Ops", password_hash="x", is_active=True)
    client = TestClient(app, follow_redirects=False)
    client.submitted = submitted
    return client


def add_message(admin, label="Cart reminder", text=TEXT):
    return admin.post("/admin/nudges/messages", data={"label": label, "text": text})


async def test_every_tab_shows(admin):
    for tab, words in {"rules": ("Stage retargeting", "Cart Created", "moved to No response",
                                 "Save rules"),
                       "sequence": ("No response", "Later", "Dead Lead", "Tapped Later",
                                    "Save sequences"),
                       "messages": ("New message", "Order Food", "Save and send for approval"),
                       "leads": ("By stage", "Messages sent", "Dead Lead")}.items():
        page = admin.get(f"/admin/nudges?tab={tab}")
        assert page.status_code == 200, tab
        for word in words:
            assert word in page.text, (tab, word)


async def test_a_new_message_is_sent_for_approval(admin, session):
    response = add_message(admin)
    assert response.status_code == 303
    assert admin.submitted == ["shero_nudge_cart_reminder"]
    cfg = await config.load(session)
    assert [m.name for m in cfg.messages] == ["shero_nudge_cart_reminder"]
    page = admin.get("/admin/nudges?tab=messages").text
    assert "Hi Asha, your Shero meal" in page and "Approved" in page

    add_message(admin, text="Your meal is waiting, {name}")         # ends on {name}: refused
    assert len((await config.load(session)).messages) == 1


async def test_turning_a_rule_on_and_nudges_on(admin, session):
    add_message(admin)
    refused = admin.post("/admin/nudges/rules", data={f"active_{CART}": "on", f"wait_{CART}": "2"})
    assert refused.status_code == 303 and not (await config.load(session)).rule("Cart Created").active

    admin.post("/admin/nudges/rules", data={
        f"active_{CART}": "on", f"wait_{CART}": "2", f"message_{CART}": "shero_nudge_cart_reminder",
        f"no_response_{CART}": ""})
    admin.post("/admin/nudges/settings", data={"enabled": "on", "send_from": "8", "send_to": "20",
                                               "timezone": "America/Chicago", "zoho_sync": "on"})
    cfg = await config.load(session)
    rule = cfg.rule("Cart Created")
    assert (rule.active, rule.wait_hours, rule.message, rule.no_response_hours) == (
        True, 2, "shero_nudge_cart_reminder", None)
    assert (cfg.enabled, cfg.send_from, cfg.send_to, cfg.timezone) == (True, 8, 20, "America/Chicago")


async def test_saving_the_sequences(admin, session):
    add_message(admin)
    admin.post("/admin/nudges/sequences", data={
        "later_active": "on", "later_first_after": "3", "later_first_unit": "days",
        "later_first_message": "shero_nudge_cart_reminder",
        "later_every_0": "5", "later_times_0": "2", "later_every_1": "20",
        "later_dead_days": "90", "later_reply": "Sure, later then!",
        "no_first_after": "30", "no_first_unit": "days", "no_dead_messages": "1"})
    cfg = await config.load(session)
    later = cfg.sequences[NudgeBucket.LATER]
    assert later.active and later.timeline(5) == ["3 days in", "+5 days", "+5 days", "+20 days",
                                                  "+20 days"]
    assert later.dead_after_days == 90 and later.reply == "Sure, later then!"
    assert cfg.sequences[NudgeBucket.NO].dead_after_messages == 1
    assert "Message 3" in admin.get("/admin/nudges?tab=sequence").text


async def test_a_message_in_use_is_not_removed(admin, session):
    add_message(admin)
    admin.post("/admin/nudges/rules", data={
        f"active_{CART}": "on", f"wait_{CART}": "2", f"message_{CART}": "shero_nudge_cart_reminder"})
    admin.post("/admin/nudges/messages/shero_nudge_cart_reminder/remove")
    assert len((await config.load(session)).messages) == 1

    admin.post("/admin/nudges/rules", data={})
    admin.post("/admin/nudges/messages/shero_nudge_cart_reminder/remove")
    assert (await config.load(session)).messages == []


async def test_the_leads_tab_shows_who_got_what(admin, session, customer):
    now = datetime.now(timezone.utc)
    session.add(NudgeLead(customer_id=customer.id, stage="Cart Created", bucket=NudgeBucket.LATER,
                          bucket_from="Cart Created", bucket_at=now, attempts=1, failures=0,
                          last_reply="Later", waiting="Waits for the send hours (9:00-21:00)"))
    session.add(NudgeSend(customer_id=customer.id, kind="stage", stage="Cart Created", attempt=1,
                          template="shero_nudge_cart_reminder", status="sent", sent_at=now,
                          delivery_status="read", reply="Later"))
    await session.flush()

    page = admin.get("/admin/nudges?tab=leads").text
    for word in ("Asha Menon", "shero_nudge_cart_reminder", "Read", "Tapped Later",
                 "Waits for the send hours"):
        assert word in page, word
    assert "Asha Menon" in admin.get("/admin/nudges?tab=leads&bucket=later").text
    assert "Asha Menon" not in admin.get("/admin/nudges?tab=leads&bucket=no").text.split("Messages sent")[0]


SMO = [{"name": "smo_m1_01", "text": "Rice is sorted for today's meal? Send ORDER FOOD on WhatsApp.",
        "buttons": [], "source": "whatsapp"},
       {"name": "smo_m1_02", "text": "Same rice. Different curry. Send ORDER FOOD to explore the menu.",
        "buttons": [], "source": "whatsapp"}]


async def test_approved_whatsapp_messages_can_be_added(admin, session, monkeypatch):
    async def addable(taken):
        return [m for m in SMO if m["name"] not in taken]

    monkeypatch.setattr(messages, "addable", addable)
    page = admin.get("/admin/nudges?tab=messages").text
    assert "2 approved messages already on WhatsApp" in page and "Add all 2" in page

    admin.post("/admin/nudges/messages/from-whatsapp", data={"names": ["smo_m1_02"]})
    admin.post("/admin/nudges/messages/from-whatsapp", data={"all": "on"})
    cfg = await config.load(session)
    assert [m.name for m in cfg.messages] == ["smo_m1_02", "smo_m1_01"]
    assert cfg.messages[0].source == "whatsapp" and cfg.messages[0].button_labels == ()
    page = admin.get("/admin/nudges?tab=messages").text
    assert "added from WhatsApp" in page and "No buttons: the customer sends ORDER FOOD" in page
    assert "Same rice. Different curry. Send ORDER FOOD" in admin.get("/admin/nudges?tab=rules").text


async def test_a_rule_waiting_for_approval_is_shown(admin, session, monkeypatch):
    add_message(admin)
    admin.post("/admin/nudges/rules", data={
        f"active_{CART}": "on", f"wait_{CART}": "2", f"message_{CART}": "shero_nudge_cart_reminder"})

    async def pending(*, fresh=False):
        return {"shero_nudge_cart_reminder": "pending"}

    monkeypatch.setattr(messages, "statuses", pending)
    page = admin.get("/admin/nudges?tab=rules").text
    assert "1 waiting" in page and "1 message your rules use is not approved by WhatsApp yet" in page
    assert 'data-state="pending"' in page
    assert admin.get("/admin/nudges/statuses").json() == {
        "statuses": {"shero_nudge_cart_reminder": "pending"}}
