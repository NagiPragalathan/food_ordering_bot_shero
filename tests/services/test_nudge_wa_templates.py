"""Each library message's WhatsApp template (services/nudges/wa_templates.py):
submitted to Meta when the message is saved, a new version when its text
changes, its status on the Messages tab, and removed when it is deleted."""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.core.exceptions import IntegrationError
from app.services.nudges import library, wa_templates

pytestmark = pytest.mark.asyncio

REAL_ROWS = wa_templates.rows        # conftest swaps it for "unknown"
TEXT = "Rice is sorted for today's meal? 🍚\n\nSend ORDER FOOD on WhatsApp. ❤️"


class Gallabox:
    """The account's templates, and the ones created."""

    def __init__(self):
        self.rows: list[dict] = []
        self.created = []
        self.down = False

    def add(self, name, text, status="approved", **extra):
        self.rows.append({"name": name, "status": status, "category": "MARKETING",
                          "channelId": settings.gallabox_channel_id,
                          "components": [{"type": "BODY", "text": text}], **extra})

    async def list_templates(self, *, channel_id=None):
        if self.down:
            raise IntegrationError("gallabox", "down")
        return list(self.rows)

    async def create_template(self, spec, *, channel_id=None):
        self.created.append(spec)
        self.add(spec.name, spec.sample_body, status="pending")
        return {"name": spec.name, "status": "pending"}


@pytest.fixture
def gallabox(monkeypatch):
    fake = Gallabox()
    monkeypatch.setattr(wa_templates, "rows", REAL_ROWS)
    monkeypatch.setattr(wa_templates.template_admin, "list_templates", fake.list_templates)
    monkeypatch.setattr(wa_templates.template_admin, "create_template", fake.create_template)
    wa_templates.forget()
    yield fake
    wa_templates.forget()


@pytest.fixture
def api(session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes.nudges import nudge_actor, router
    from app.db.session import get_session

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[nudge_actor] = lambda: "ops@test"
    return TestClient(app)


def new_message(**extra):
    return {"code": "M1-10", "pool": "SMO", "month": 1, "title": "Rice", "body": TEXT, **extra}


async def test_saving_a_new_message_submits_it_to_meta(api, gallabox):
    made = api.post("/api/nudges/messages", json=new_message()).json()

    assert made["wa_submit"] == {"submitted": True, "name": "smo_m1_10", "status": "pending"}
    (spec,) = gallabox.created
    assert (spec.name, spec.category, spec.params) == ("smo_m1_10", "MARKETING", ())
    assert spec.sample_body == TEXT
    assert made["wa"]["state"] == "pending" and made["wa_template"] == "smo_m1_10"

    gallabox.rows[0]["status"] = "APPROVED"
    wa_templates.forget()
    listed = api.get("/api/nudges/messages").json()
    assert listed["messages"][0]["wa"]["state"] == "approved"
    assert listed["templates"]["summary"]["approved"] == 1
    assert listed["templates"]["reachable"] is True


async def test_a_placeholder_becomes_a_template_value(api, gallabox):
    made = api.post("/api/nudges/messages", json=new_message(
        body="Hi {first_name}, rice is sorted? Send ORDER FOOD.")).json()
    (spec,) = gallabox.created
    assert spec.sample_body == "Hi {{1}}, rice is sorted? Send ORDER FOOD."
    assert spec.params == ("first_name",)
    assert made["wa_params"] == ["{first_name}"]


async def test_a_template_already_there_with_the_same_text_is_not_sent_again(api, gallabox):
    gallabox.add("smo_m1_10", TEXT, status="pending")
    made = api.post("/api/nudges/messages", json=new_message(wa_template="smo_m1_10")).json()
    assert made["wa_submit"]["already"] is True and gallabox.created == []
    assert made["wa"]["state"] == "pending"


async def test_changed_text_is_a_new_version_and_the_old_one_is_listed(api, gallabox, session):
    made = api.post("/api/nudges/messages", json=new_message()).json()
    gallabox.rows[0]["status"] = "approved"

    edited = api.patch(f"/api/nudges/messages/{made['id']}",
                       json={"body": TEXT.replace("Rice", "Roti")}).json()

    assert edited["wa_submit"]["name"] == "smo_m1_10_v2"
    assert edited["wa_template"] == "smo_m1_10_v2" and edited["wa"]["state"] == "pending"
    listed = api.get("/api/nudges/messages").json()["templates"]["leftovers"]
    assert [(i["name"], i["why"]) for i in listed] == [
        ("smo_m1_10", "text changed, replaced by a new version")]
    # Switching it off is not an edit of the text: nothing is submitted.
    api.patch(f"/api/nudges/messages/{made['id']}", json={"is_active": False})
    assert len(gallabox.created) == 2


async def test_a_copy_gets_its_own_template_and_never_lists_the_original(api, gallabox):
    made = api.post("/api/nudges/messages", json=new_message()).json()
    copy = api.post(f"/api/nudges/messages/{made['id']}/duplicate").json()
    assert copy["wa_template"] is None and copy["wa"]["state"] == "none"

    edited = api.patch(f"/api/nudges/messages/{copy['id']}",
                       json={"body": "Roti tonight? Send ORDER FOOD."}).json()
    assert edited["wa_submit"]["name"] == "smo_m1_10_copy"
    assert api.get("/api/nudges/messages").json()["templates"]["leftovers"] == []


async def test_a_template_whose_text_no_longer_matches_is_not_used(gallabox, session):
    message = await library.create_message(session, new_message(wa_template="smo_m1_10"),
                                           actor="test")
    gallabox.add("smo_m1_10", "Older words. Send ORDER FOOD.")
    assert await wa_templates.matches(message) is False
    assert wa_templates.state(message, await wa_templates.rows())["state"] == "text_changed"
    message.body = "Older words. Send ORDER FOOD."
    assert await wa_templates.matches(message) is True


async def test_rejected_shows_the_reason_and_submit_tries_a_new_version(api, gallabox):
    gallabox.add("smo_m1_10", TEXT, status="REJECTED", rejected_reason="INVALID_FORMAT")
    made = api.post("/api/nudges/messages", json=new_message(wa_template="smo_m1_10")).json()
    # Saving submitted a new version straight away, as the old one was refused.
    assert made["wa_submit"]["name"] == "smo_m1_10_v2"
    gallabox.rows[-1]["status"] = "rejected"
    gallabox.rows[-1]["rejected_reason"] = "PROMOTIONAL"
    wa_templates.forget()
    shown = api.get(f"/api/nudges/messages/{made['id']}").json()["wa"]
    assert (shown["state"], shown["reason"]) == ("rejected", "PROMOTIONAL")

    again = api.post(f"/api/nudges/messages/{made['id']}/submit").json()
    assert again["wa_submit"]["name"] == "smo_m1_10_v3"


async def test_deleting_lists_the_template_when_meta_cannot_be_asked(api, gallabox):
    made = api.post("/api/nudges/messages", json=new_message()).json()
    gone = api.delete(f"/api/nudges/messages/{made['id']}").json()
    assert gone["template"] == {"template": "smo_m1_10", "deleted": False, "listed": True}
    left = api.get("/api/nudges/messages").json()["templates"]
    assert [i["name"] for i in left["leftovers"]] == ["smo_m1_10"] and left["can_delete"] is False

    api.post("/api/nudges/templates/leftovers/dismiss", json={"name": "smo_m1_10"})
    assert api.get("/api/nudges/messages").json()["templates"]["leftovers"] == []


async def test_deleting_deletes_it_on_meta_when_the_keys_are_set(api, gallabox, monkeypatch):
    deleted = []

    async def meta_delete(name):
        deleted.append(name)
        return None
    monkeypatch.setattr(settings, "meta_system_user_token", "token")
    monkeypatch.setattr(settings, "meta_waba_id", "1234")
    monkeypatch.setattr(wa_templates, "_meta_delete", meta_delete)

    made = api.post("/api/nudges/messages", json=new_message()).json()
    gone = api.delete(f"/api/nudges/messages/{made['id']}").json()
    assert gone["template"] == {"template": "smo_m1_10", "deleted": True} and deleted == ["smo_m1_10"]
    assert api.get("/api/nudges/messages").json()["templates"]["leftovers"] == []


async def test_after_an_import_the_missing_ones_are_submitted(gallabox, session):
    gallabox.add("smo_m1_01", TEXT, status="pending")
    await library.create_message(session, new_message(code="M1-01", wa_template="smo_m1_01"),
                                 actor="test")
    await library.create_message(session, new_message(code="M1-02"), actor="test")
    await library.create_message(session, new_message(code="M1-03", body="New words. Send ORDER FOOD."),
                                 actor="test")

    assert await wa_templates.submit_missing(session, actor="test") == 2
    assert sorted(s.name for s in gallabox.created) == ["smo_m1_02", "smo_m1_03"]


async def test_gallabox_down_saves_the_message_and_says_so(api, gallabox):
    gallabox.down = True
    made = api.post("/api/nudges/messages", json=new_message())
    assert made.status_code == 201
    assert made.json()["wa_submit"]["submitted"] is False
    assert "Gallabox could not be reached" in made.json()["wa_submit"]["error"]
    # Not named yet, so it shows as not submitted, with Submit for approval.
    assert made.json()["wa"]["state"] == "none" and made.json()["wa_template"] is None


def test_the_page_shows_the_template_status():
    from pathlib import Path

    page = Path("app/templates/admin/nudges.html").read_text(encoding="utf-8")
    for there in ("templateStatus(m)", "leftoversPanel()", 'data-mact="submit"', "WhatsApp templates: ",
                  "Waiting for Meta", "mf-wa", "submitToast"):
        assert there in page, there
    assert "line-clamp-5 whitespace-pre-line\">${wa(m.body)}" not in page   # the whole text shows
