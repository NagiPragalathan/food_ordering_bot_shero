"""The email question, switched on or off on the Bot replies page."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db.models import AdminUser, ConversationStep, Customer, LeadStage
from app.services.conversation import prompts as p
from app.services.conversation.engine import handle_event
from tests.conversation.test_happy_path import _state, bot, text  # noqa: F401


@pytest.fixture
def no_email(monkeypatch):
    monkeypatch.setattr(settings, "ask_customer_email", "off")


async def test_switched_off_the_name_leads_straight_to_the_menu(session, outlet, menu, bot, no_email):
    await handle_event(session, text("hi"))
    await handle_event(session, text("Asha Menon"))

    customer, conversation = await _state(session)
    assert conversation.step == ConversationStep.MAIN_MENU
    assert customer.name == "Asha Menon" and customer.email is None
    assert customer.lead_stage == LeadStage.DETAILS_CAPTURED
    assert not any("email" in m.body.lower() for m in bot.sent)

    # Coming back later: greeted by name, never asked for the email.
    bot.clear()
    await handle_event(session, text("hi"))
    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.MAIN_MENU
    assert not any("email" in m.body.lower() for m in bot.sent)


async def test_switched_on_the_email_is_still_asked(session, outlet, menu, bot, monkeypatch):
    monkeypatch.setattr(settings, "ask_customer_email", "")          # nothing saved = on
    await handle_event(session, text("hi"))
    await handle_event(session, text("Asha Menon"))

    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.AWAIT_EMAIL
    assert bot.last().body == p.ASK_EMAIL.format(name="Asha Menon")


@pytest.mark.parametrize("answer, email", [("ok", None), ("asha@example.com", "asha@example.com")])
async def test_asked_just_before_it_was_switched_off(session, outlet, menu, bot, monkeypatch,
                                                     answer, email):
    monkeypatch.setattr(settings, "ask_customer_email", "")
    await handle_event(session, text("hi"))
    await handle_event(session, text("Asha Menon"))
    monkeypatch.setattr(settings, "ask_customer_email", "off")

    await handle_event(session, text(answer))

    customer, conversation = await _state(session)
    assert conversation.step == ConversationStep.MAIN_MENU and customer.email == email


def test_details_need_the_email_only_while_it_is_asked(monkeypatch):
    customer = Customer(whatsapp_number="17325550142", name="Asha")
    monkeypatch.setattr(settings, "ask_customer_email", "on")
    assert not customer.has_details
    monkeypatch.setattr(settings, "ask_customer_email", "off")
    assert customer.has_details


async def test_the_switch_on_the_bot_replies_page(session, monkeypatch):
    from app.admin import router
    from app.admin.deps import require_admin
    from app.db.session import get_session

    monkeypatch.setattr(settings, "ask_customer_email", "")      # restored after the test
    monkeypatch.setattr(settings, "admin_session_secret", "test-secret")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_admin] = lambda: AdminUser(
        email="ops@test", name="Ops", password_hash="x", is_active=True)
    client = TestClient(app, follow_redirects=False)

    page = client.get("/admin/bot-replies").text
    assert "Ask for their email" in page and 'name="ask_email" value="1" checked' in page

    assert client.post("/admin/bot-replies/email", data={}).status_code == 303
    assert not settings.asks_customer_email
    assert "never asks for an email" in client.get("/admin/bot-replies").text

    client.post("/admin/bot-replies/email", data={"ask_email": "1"})
    assert settings.asks_customer_email
