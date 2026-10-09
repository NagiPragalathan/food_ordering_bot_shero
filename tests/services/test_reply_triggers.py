"""Keyword triggers: with "Reply to any message" off, a chat only starts the
bot on a trigger keyword (services/reply_triggers.py).

What must hold: a stranger's "when is my order coming?" is left for the team,
while a customer half-way through ordering can still type their address.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from starlette.datastructures import FormData

from app.admin.routes import bot_replies
from app.core.config import settings
from app.db.models import Conversation, ConversationStep, Customer
from app.schemas.inbound import InboundEvent, InboundKind
from app.services import reply_triggers as rt

PHONE = "17325550142"
NOW = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)


def _set(monkeypatch, *triggers: tuple[str, str], mode: str = rt.KEYWORDS):
    monkeypatch.setattr(settings, "bot_reply_trigger", mode)
    monkeypatch.setattr(settings, "bot_trigger_keywords",
                        rt.dump([rt.Trigger(k, m) for k, m in triggers]))


@pytest.fixture
def keywords(monkeypatch):
    _set(monkeypatch, ("hi", rt.EXACT), ("order", rt.CONTAINS), ("menu", rt.STARTS))


def text(body: str) -> InboundEvent:
    return InboundEvent(whatsapp_number=PHONE, message_id="m1", kind=InboundKind.TEXT, text=body)


async def _customer_at(session, step: ConversationStep, *, quiet_for: timedelta) -> None:
    customer = Customer(whatsapp_number=PHONE)
    session.add(customer)
    await session.flush()
    session.add(Conversation(customer_id=customer.id, step=str(step),
                             last_message_at=NOW - quiet_for, updated_at=NOW - quiet_for))
    await session.flush()


# --- matching ------------------------------------------------------------------------
@pytest.mark.parametrize("message, expected", [
    ("hi", True), ("Hi!!", True), ("  HI 👋 ", True), ("hi there", False), ("this", False),
])
def test_exact_is_the_whole_message(message, expected):
    assert rt.matches(rt.Trigger("hi", rt.EXACT), message) is expected


@pytest.mark.parametrize("message, expected", [
    ("I want to order", True), ("Order, please", True), ("can I place an order?", True),
    ("reorder", False), ("orders", False),
])
def test_contains_is_whole_words(message, expected):
    assert rt.matches(rt.Trigger("order", rt.CONTAINS), message) is expected


def test_a_phrase_can_be_a_trigger():
    trigger = rt.Trigger("Order Food", rt.CONTAINS)
    assert rt.matches(trigger, "hey, can I order   food today?")
    assert not rt.matches(trigger, "food order")


def test_starts_with():
    trigger = rt.Trigger("menu", rt.STARTS)
    assert rt.matches(trigger, "Menu please") and rt.matches(trigger, "menu")
    assert not rt.matches(trigger, "the menu") and not rt.matches(trigger, "menus")


# --- the stored list -----------------------------------------------------------------
def test_nothing_saved_replies_to_any_message():
    """A missing setting must never silently switch the bot off."""
    assert rt.mode() == rt.ANY


def test_keyword_mode_with_no_keywords_still_replies(monkeypatch):
    _set(monkeypatch)
    assert rt.mode() == rt.ANY


def test_switching_back_on_keeps_the_keywords(monkeypatch):
    _set(monkeypatch, ("hi", rt.EXACT), mode=rt.ANY)
    assert rt.mode() == rt.ANY and rt.triggers() == [rt.Trigger("hi", rt.EXACT)]


def test_a_broken_or_repeated_list_is_cleaned(monkeypatch):
    monkeypatch.setattr(settings, "bot_trigger_keywords", "not json")
    assert rt.triggers() == []
    monkeypatch.setattr(settings, "bot_trigger_keywords",
                        '[{"keyword": "Hi", "match": "exact"}, {"keyword": "hi!", "match": "exact"},'
                        ' {"keyword": "x", "match": "regex"}, {"keyword": "!!", "match": "exact"}]')
    assert rt.triggers() == [rt.Trigger("Hi", rt.EXACT)]


# --- who gets answered ---------------------------------------------------------------
async def test_any_message_mode_answers_everything(session):
    assert (await rt.decide(session, text("random"), NOW)).answer


async def test_a_new_customer_needs_a_trigger(session, keywords):
    ignored = await rt.decide(session, text("is anyone there"), NOW)
    assert not ignored.answer
    started = await rt.decide(session, text("Hi"), NOW)
    assert started.answer and started.fresh_start


async def test_mid_order_everything_is_answered(session, keywords):
    """They have to be able to type their address."""
    await _customer_at(session, ConversationStep.AWAIT_ADDRESS, quiet_for=timedelta(minutes=5))
    decision = await rt.decide(session, text("12 Oak Street, Edison"), NOW)
    assert decision.answer and decision.reason == "order in progress"


async def test_a_trigger_mid_order_does_not_restart(session, keywords):
    await _customer_at(session, ConversationStep.CART_REVIEW, quiet_for=timedelta(minutes=5))
    decision = await rt.decide(session, text("I want to order more"), NOW)
    assert decision.answer and not decision.fresh_start


async def test_an_old_unfinished_chat_needs_a_trigger_and_starts_over(session, keywords):
    await _customer_at(session, ConversationStep.CUISINE_MENU, quiet_for=timedelta(hours=30))
    assert not (await rt.decide(session, text("thanks"), NOW)).answer
    decision = await rt.decide(session, text("menu please"), NOW)
    assert decision.answer and decision.fresh_start


async def test_after_an_order_the_team_gets_the_chat(session, keywords):
    await _customer_at(session, ConversationStep.COMPLETED, quiet_for=timedelta(minutes=10))
    assert not (await rt.decide(session, text("when will it arrive?"), NOW)).answer


async def test_a_chat_with_a_person_stays_with_them(session, keywords):
    await _customer_at(session, ConversationStep.HANDED_OVER, quiet_for=timedelta(minutes=10))
    assert not (await rt.decide(session, text("ok thanks"), NOW)).answer


async def test_button_taps_and_the_bots_own_messages_always_work(session, keywords):
    tap = InboundEvent(whatsapp_number=PHONE, message_id="m2", kind=InboundKind.REPLY,
                       reply_id="Great")
    assert (await rt.decide(session, tap, NOW)).answer
    assert (await rt.decide(session, text("New menu link"), NOW)).answer
    assert (await rt.decide(session, text("Continue on WhatsApp"), NOW)).answer


async def test_deciding_never_creates_a_customer(session, keywords):
    await rt.decide(session, text("hello?"), NOW)
    assert (await session.execute(select(func.count(Customer.id)))).scalar_one() == 0


# --- the engine ----------------------------------------------------------------------
async def test_a_fresh_start_greets_instead_of_answering_the_old_question(session, monkeypatch):
    """"order food" on a chat parked at the email question two days ago
    must not be read as an (invalid) email."""
    from app.integrations.gallabox.sender import use_sender
    from app.services.conversation.engine import handle_event
    from tests.conversation.test_happy_path import FakeGallabox

    await _customer_at(session, ConversationStep.AWAIT_EMAIL, quiet_for=timedelta(days=2))
    fake = FakeGallabox()
    with use_sender(fake):
        await handle_event(session, text("order food"), fresh_start=True)

    conversation = (await session.execute(select(Conversation))).scalar_one()
    assert conversation.step == str(ConversationStep.AWAIT_NAME)     # welcome, asks the name
    assert "email" not in fake.last().body.lower()


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
    response = await bot_replies.save_triggers(_FormRequest(items), session=session,
                                               current_user=_Admin())
    return response


async def test_saving_keywords_switches_to_keyword_mode(session):
    await _save(session, [("keyword", "Hi"), ("match", "exact"),
                          ("keyword", "order food"), ("match", "contains"),
                          ("keyword", ""), ("match", "contains")])
    assert rt.mode() == rt.KEYWORDS
    assert rt.triggers() == [rt.Trigger("Hi", rt.EXACT), rt.Trigger("order food", rt.CONTAINS)]


async def test_keyword_mode_without_keywords_is_refused(session):
    await _save(session, [("keyword", " "), ("match", "exact")])
    assert rt.mode() == rt.ANY and settings.bot_reply_trigger in ("", rt.ANY)


async def test_switching_on_keeps_the_list(session):
    await _save(session, [("keyword", "hi"), ("match", "exact")])
    await _save(session, [("any_message", "1")])
    assert rt.mode() == rt.ANY and rt.triggers() == [rt.Trigger("hi", rt.EXACT)]


async def test_an_unknown_match_type_is_refused(session):
    await _save(session, [("keyword", "hi"), ("match", "regex")])
    assert rt.mode() == rt.ANY and rt.triggers() == []


def test_the_page_renders_the_switch_and_rows(monkeypatch):
    from app.templating import templates

    _set(monkeypatch, ("hi", rt.EXACT), ("order food", rt.CONTAINS))
    html = templates.env.get_template("admin/_bot_triggers.html").render(
        url_for=lambda name, **_: f"/{name}", trigger_mode=rt.mode(), triggers=rt.triggers(),
        matches=rt.MATCHES, replies=rt.REPLIES, active_hours=rt.ACTIVE_HOURS, message_query="hi",
        message_check=bot_replies._message_check("hi"))
    assert 'name="any_message"' in html and "checked" not in html.split('id="any-message"')[1][:80]
    assert 'value="order food"' in html and "Keywords only" in html
    assert "Starts the bot" in html


# --- the webhook ---------------------------------------------------------------------
async def _webhook(session, monkeypatch, body: str) -> tuple[dict, list]:
    from app.api.routes import webhooks_gallabox

    handled = []

    async def spy(session, event, **kwargs):
        handled.append(kwargs)
        return True

    monkeypatch.setattr(webhooks_gallabox, "handle_event", spy)
    monkeypatch.setattr(webhooks_gallabox, "verify_gallabox_token", lambda t: True)

    class FakeRequest:
        headers: dict = {}

        async def json(self):
            return {"whatsapp": {"from": PHONE, "type": "text", "text": {"body": body}}}

    result = await webhooks_gallabox.gallabox_webhook(
        FakeRequest(), session=session, authorization="t",
        x_gallabox_token=None, x_webhook_secret=None, token=None)
    return result, handled


async def test_the_webhook_leaves_a_message_without_a_trigger_for_the_team(session, keywords,
                                                                         monkeypatch):
    result, handled = await _webhook(session, monkeypatch, "is my parcel late")
    assert result == {"status": "ignored", "reason": "no trigger keyword"} and handled == []


async def test_the_webhook_starts_the_bot_on_a_trigger(session, keywords, monkeypatch):
    result, handled = await _webhook(session, monkeypatch, "Hi!")
    assert result["status"] == "ok" and handled == [{"fresh_start": True, "reply": "both"}]


# --- what the bot answers a trigger with --------------------------------------------
def test_each_keyword_keeps_its_reply(monkeypatch):
    _set(monkeypatch, ("hi", rt.EXACT))
    monkeypatch.setattr(settings, "bot_trigger_keywords", rt.dump(
        [rt.Trigger("hi", rt.EXACT, rt.ORDER_NOW), rt.Trigger("order", rt.CONTAINS)]))
    assert [t.reply for t in rt.triggers()] == [rt.ORDER_NOW, rt.BOTH]


def test_a_list_saved_before_replies_existed_means_both(monkeypatch):
    monkeypatch.setattr(settings, "bot_reply_trigger", rt.KEYWORDS)
    monkeypatch.setattr(settings, "bot_trigger_keywords", '[{"keyword": "hi", "match": "exact"}]')
    assert rt.triggers() == [rt.Trigger("hi", rt.EXACT, rt.BOTH)]


def test_the_reply_only_applies_in_keyword_mode(monkeypatch):
    assert rt.welcome_reply(rt.WHATSAPP) == rt.BOTH          # Reply to any message is on
    _set(monkeypatch, ("hi", rt.EXACT))
    assert rt.welcome_reply(rt.WHATSAPP) == rt.WHATSAPP
    assert rt.welcome_reply(None) == rt.BOTH and rt.welcome_reply("rubbish") == rt.BOTH


@pytest.mark.parametrize("reply, kinds", [
    (rt.BOTH, ["cta_url", "buttons"]),
    (rt.ORDER_NOW, ["cta_url"]),
    (rt.WHATSAPP, ["buttons"]),
])
async def test_the_welcome_follows_the_triggers_reply(session, monkeypatch, reply, kinds):
    from app.integrations.gallabox.sender import use_sender
    from app.services.conversation.engine import handle_event
    from tests.conversation.test_happy_path import FakeGallabox

    monkeypatch.setattr(settings, "bot_reply_trigger", rt.KEYWORDS)
    monkeypatch.setattr(settings, "bot_trigger_keywords",
                        rt.dump([rt.Trigger("hi", rt.EXACT, reply)]))
    customer = Customer(whatsapp_number=PHONE, name="Nagi", email="nagi@example.com")
    session.add(customer)
    await session.flush()

    fake = FakeGallabox()
    with use_sender(fake):
        await handle_event(session, text("hi"), fresh_start=True, reply=reply)

    assert [m.kind for m in fake.sent] == kinds
    first = fake.sent[0]
    if reply == rt.WHATSAPP:
        assert "Nagi" in first.body and "Order right here on WhatsApp" in first.body
        assert first.payload["buttons"] == [("Continue on WhatsApp", "Continue on WhatsApp")]
    else:
        assert first.payload["url"] == "https://www.shero.us/"
