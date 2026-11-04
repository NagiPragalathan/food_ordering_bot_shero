"""The welcome menu: in keyword mode, a new chat gets the menu first, and its
buttons start the ordering bot or are left to a Gallabox flow
(services/welcome_menu.py).

What must hold, from the chat that went wrong: Gallabox's own menu answered
every message and the bot answered Gallabox's buttons, so a customer typing
their name got both bots at once. Now one menu goes, once; a Gallabox button
gets nothing from the bot, nor does anything in that flow; and the ordering
bot still takes the name it asked for.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from starlette.datastructures import FormData

from app.admin.routes import bot_replies
from app.core.config import settings
from app.db.models import Conversation, ConversationStep, Customer, WelcomeMenuChat
from app.schemas.inbound import InboundEvent, InboundKind
from app.services import reply_triggers as rt
from app.services import welcome_menu as wm

PHONE = "17325550142"
NOW = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)
GALLABOX_BUTTON_ID = "47339974-f083-433c-85d7-9061cee2ea57"     # a Gallabox flow's own button


@pytest.fixture
def menu_on(monkeypatch):
    monkeypatch.setattr(settings, "bot_reply_trigger", rt.KEYWORDS)
    monkeypatch.setattr(settings, "bot_trigger_keywords",
                        rt.dump([rt.Trigger("ORDER FOOD", rt.CONTAINS, rt.WHATSAPP)]))
    monkeypatch.setattr(settings, "bot_welcome_menu", wm.dump(wm.Menu(enabled=True)))


def text(body: str, number: str = PHONE) -> InboundEvent:
    return InboundEvent(whatsapp_number=number, message_id="m1", kind=InboundKind.TEXT, text=body)


def tap(button_id: str, title: str, number: str = PHONE) -> InboundEvent:
    return InboundEvent(whatsapp_number=number, message_id="m2", kind=InboundKind.REPLY,
                        reply_id=button_id, reply_title=title)


# --- the stored menu -----------------------------------------------------------------
def test_nothing_saved_is_off_with_gallaboxs_menu_ready():
    menu = wm.load()
    assert not menu.enabled and menu.text.startswith("Hi!\n\nThanks for reaching out to Shero.")
    assert [(b.label, b.goes_to) for b in menu.buttons] == [
        ("Party Order", wm.GALLABOX), ("Sweet & Snacks", wm.GALLABOX),
        ("Single Meal Order", rt.WHATSAPP)]
    assert wm.parse(wm.dump(wm.Menu(enabled=True))) == wm.Menu(enabled=True)


def test_buttons_match_the_whole_label():
    menu = wm.Menu(enabled=True)
    assert menu.button("party order").label == "Party Order"
    assert menu.button(" Sweet & Snacks!") .label == "Sweet & Snacks"
    assert menu.button("party order for 40") is None and menu.button("") is None


def test_whatsapps_limits_are_checked():
    too_long = wm.Menu(buttons=(wm.MenuButton("A label far too long here", wm.GALLABOX),))
    twins = wm.Menu(buttons=(wm.MenuButton("Party Order", wm.GALLABOX),
                             wm.MenuButton("party order!", rt.BOTH)))
    assert any("longer than 20" in p for p in wm.problems(too_long))
    assert "Each button needs a different label." in wm.problems(twins)
    assert wm.problems(wm.Menu(buttons=())) == ["Add at least one button."]
    assert wm.problems(wm.Menu()) == []


# --- who gets what ------------------------------------------------------------------
async def test_off_changes_nothing(session, monkeypatch):
    monkeypatch.setattr(settings, "bot_reply_trigger", rt.KEYWORDS)
    monkeypatch.setattr(settings, "bot_trigger_keywords", rt.dump([rt.Trigger("order food")]))
    decision = await rt.decide(session, text("Hi"), NOW)
    assert (decision.answer, decision.action) == (False, "")


async def test_only_in_keyword_mode(session, menu_on, monkeypatch):
    monkeypatch.setattr(settings, "bot_reply_trigger", rt.ANY)
    decision = await rt.decide(session, text("Hi"), NOW)
    assert decision.answer and decision.action == ""


async def test_a_new_chat_gets_the_menu_once_a_day(session, menu_on):
    assert (await rt.decide(session, text("Hi"), NOW)).action == wm.SEND
    session.add(WelcomeMenuChat(whatsapp_number=PHONE, menu_sent_at=NOW - timedelta(hours=2)))
    await session.flush()
    again = await rt.decide(session, text("hello?"), NOW)
    assert (again.answer, again.action) == (False, "")
    assert (await rt.decide(session, text("Hi"), NOW + timedelta(hours=23))).action == wm.SEND


async def test_a_trigger_skips_the_menu(session, menu_on):
    decision = await rt.decide(session, text("I want to order food"), NOW)
    assert decision.answer and decision.fresh_start and decision.reply == rt.WHATSAPP


async def test_the_ordering_buttons_start_the_bot(session, menu_on):
    decision = await rt.decide(session, tap("Single Meal Order", "Single Meal Order"), NOW)
    assert (decision.answer, decision.fresh_start, decision.reply) == (True, True, rt.WHATSAPP)
    # Gallabox's old card's button, with its own id: the words decide.
    old = await rt.decide(session, tap("bce57b95-6b18-45d1-96c4-c8a4ed663494", "Single Meal Order"), NOW)
    assert old.answer and old.reason == "welcome menu"


async def test_a_gallabox_button_is_left_to_gallabox(session, menu_on):
    decision = await rt.decide(session, tap("Party Order", "Party Order"), NOW)
    assert (decision.answer, decision.action, decision.label) == (
        False, wm.LEFT_TO_GALLABOX, "Party Order")


async def test_inside_a_gallabox_flow_the_bot_keeps_out(session, menu_on):
    session.add(WelcomeMenuChat(whatsapp_number=PHONE, gallabox_flow="Party Order",
                                gallabox_at=NOW - timedelta(hours=1)))
    await session.flush()
    for event in (text("40 people, Saturday"), text("Hi"), tap(GALLABOX_BUTTON_ID, "Veg")):
        decision = await rt.decide(session, event, NOW)
        assert (decision.answer, decision.action) == (False, ""), event
    assert (await rt.decide(session, text("order food"), NOW)).answer   # back to ordering
    # A day later the chat is new again.
    assert (await rt.decide(session, text("Hi"), NOW + timedelta(hours=25))).action == wm.SEND


async def _customer_at(session, step, *, quiet_for: timedelta) -> None:
    customer = Customer(whatsapp_number=PHONE, name="Mani", email="mani@example.com")
    session.add(customer)
    await session.flush()
    session.add(Conversation(customer_id=customer.id, step=str(step),
                             last_message_at=NOW - quiet_for, updated_at=NOW - quiet_for))
    await session.flush()


async def test_no_menu_while_the_bot_is_in_the_chat(session, menu_on):
    await _customer_at(session, ConversationStep.MAIN_MENU, quiet_for=timedelta(minutes=5))
    assert (await rt.decide(session, text("Hi"), NOW)).action == ""


async def test_the_bot_still_takes_the_name_it_asked_for(session, menu_on):
    await _customer_at(session, ConversationStep.AWAIT_NAME, quiet_for=timedelta(minutes=1))
    decision = await rt.decide(session, text("Mani"), NOW)
    assert decision.answer and decision.reason == "order in progress"


async def test_a_customer_back_after_days_gets_the_menu(session, menu_on):
    await _customer_at(session, ConversationStep.COMPLETED, quiet_for=timedelta(days=3))
    assert (await rt.decide(session, text("Hi"), NOW)).action == wm.SEND


# --- through the webhook ------------------------------------------------------------
@pytest.fixture
async def chat(session, menu_on, monkeypatch):
    from app.api.routes import webhooks_gallabox
    from app.integrations.gallabox.sender import use_sender
    from tests.conversation.test_happy_path import FakeGallabox

    monkeypatch.setattr(webhooks_gallabox, "verify_gallabox_token", lambda t: True)
    fake, count = FakeGallabox(), iter(range(1, 1000))

    async def send(message: str | None = None, *, button: tuple[str, str] | None = None,
                   number: str = PHONE) -> list:
        whatsapp = ({"type": "interactive", "interactive": {"button_reply": {
            "id": button[0], "title": button[1]}}} if button
            else {"type": "text", "text": {"body": message}})

        class FakeRequest:
            headers: dict = {}

            async def json(self):
                return {"whatsapp": {"from": number, "id": f"wamid.{next(count)}", **whatsapp}}

        before = len(fake.sent)
        with use_sender(fake):
            await webhooks_gallabox.gallabox_webhook(
                FakeRequest(), session=session, authorization="t",
                x_gallabox_token=None, x_webhook_secret=None, token=None)
        return fake.sent[before:]

    return send


async def test_the_reported_chat_now_gets_one_bot_at_a_time(session, chat):
    menu = await chat("Hi")
    assert [m.kind for m in menu] == ["buttons"]
    assert menu[0].body.startswith("Hi!\n\nThanks for reaching out to Shero.")
    assert menu[0].payload["buttons"] == [("Party Order", "Party Order"),
                                          ("Sweet & Snacks", "Sweet & Snacks"),
                                          ("Single Meal Order", "Single Meal Order")]
    assert await chat("hello?") == []                                  # once, not every message

    welcome = await chat(button=("Single Meal Order", "Single Meal Order"))
    assert "full name" in welcome[-1].body                             # a new customer: their name
    after_name = await chat("Mani")                                    # no keyword, still the bot's
    assert after_name and not any("reaching out" in m.body for m in after_name)
    assert (await session.execute(select(Customer.name))).scalar_one() == "Mani"


async def test_party_order_is_gallaboxs_until_they_ask_for_food(session, chat):
    await chat("Hi")
    assert await chat(button=("Party Order", "Party Order")) == []
    row = await session.get(WelcomeMenuChat, PHONE)
    assert row.gallabox_flow == "Party Order" and row.gallabox_at is not None
    assert await chat("40 people on Saturday") == []                   # the flow's question
    assert await chat(button=(GALLABOX_BUTTON_ID, "Veg")) == []        # the flow's button
    assert await chat("Hi") == []                                      # no menu over the flow

    assert await chat("ORDER FOOD")                                    # back to ordering
    assert (await session.get(WelcomeMenuChat, PHONE)).gallabox_at is None
    assert await chat("Mani")                                          # their name: the bot's


async def test_a_failed_send_is_tried_again(session, chat, monkeypatch):
    from app.core.exceptions import IntegrationError
    from app.services import welcome_menu

    class Down:
        async def send_buttons(self, *a, **k):
            raise IntegrationError("gallabox", "down")

    with monkeypatch.context() as patch:
        patch.setattr(welcome_menu, "current_sender", lambda: Down())
        await chat("Hi")
    assert await session.get(WelcomeMenuChat, PHONE) is None
    assert await chat("Hi")                                            # the next message gets it


async def test_the_menu_creates_no_customer(session, chat):
    await chat("Hi")
    await chat(button=("Party Order", "Party Order"))
    assert (await session.execute(select(Customer))).first() is None


# --- the admin page ------------------------------------------------------------------
class _FormRequest:
    def __init__(self, items: list[tuple[str, str]]):
        self._form = FormData(items)

    async def form(self):
        return self._form

    def url_for(self, name, **_):
        return "/admin/bot-replies"


class _Admin:
    email = "admin@shero.us"


async def _save(session, items):
    return await bot_replies.save_welcome_menu(_FormRequest(items), session=session,
                                               current_user=_Admin())


async def test_saving_the_menu(session):
    await _save(session, [("enabled", "1"), ("text", "Hello from Shero!\r\nWhat would you like?"),
                          ("label", "Party Order"), ("goes_to", wm.GALLABOX),
                          ("label", " Single  Meal Order "), ("goes_to", rt.BOTH),
                          ("label", ""), ("goes_to", wm.GALLABOX)])
    menu = wm.load()
    assert menu.enabled and menu.text == "Hello from Shero!\nWhat would you like?"
    assert [(b.label, b.goes_to) for b in menu.buttons] == [
        ("Party Order", wm.GALLABOX), ("Single Meal Order", rt.BOTH)]


async def test_a_menu_over_whatsapps_limits_is_refused(session):
    await _save(session, [("enabled", "1"), ("text", "Hi"),
                          ("label", "Party Order and more things"), ("goes_to", wm.GALLABOX)])
    assert not wm.load().enabled


async def test_the_whole_page_renders(session, menu_on):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.admin import router
    from app.admin.deps import require_admin
    from app.db.models import AdminUser
    from app.db.session import get_session

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_admin] = lambda: AdminUser(
        email="ops@shero.test", name="Ops", password_hash="x", is_active=True)
    client = TestClient(app, follow_redirects=False)
    page = client.get("/admin/bot-replies?message=Party+Order")
    assert page.status_code == 200
    assert "Welcome menu" in page.text and "Set up in Gallabox" in page.text
    assert "Left to Gallabox" in page.text                             # the message tester
    saved = client.post("/admin/bot-replies/welcome-menu", data={
        "text": "Hi!", "label": ["Single Meal Order"], "goes_to": [rt.WHATSAPP]})
    assert saved.status_code == 303 and not wm.load().enabled


def test_the_page_shows_the_menu_and_the_gallabox_setup(monkeypatch):
    from app.templating import templates

    monkeypatch.setattr(settings, "bot_welcome_menu", wm.dump(wm.Menu(enabled=True)))
    html = templates.env.get_template("admin/_welcome_menu.html").render(
        url_for=lambda name, **_: f"/{name}", welcome=wm.load(), goes_to=wm.GOES_TO,
        label_limit=wm.LABEL_LIMIT, text_limit=wm.TEXT_LIMIT, trigger_mode=rt.KEYWORDS)
    assert 'value="Sweet &amp; Snacks"' in html and "A Gallabox flow" in html
    assert "&ldquo;Party Order&rdquo;, &ldquo;Sweet &amp; Snacks&rdquo;" in html
    assert "<b>On.</b>" in html
