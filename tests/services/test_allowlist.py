"""Test mode: the bot answers only the numbers in BOT_ALLOWED_NUMBERS.

Written after the bot, left open on the live business number, asked 26 real
customers for their name and email in an hour - people who had written in
about actual orders. The cost of this going wrong is intercepting strangers,
so the tests lean on what must NOT get through.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.services import allowlist

ME = "917401268091"
STRANGER = "917695828197"      # a real customer from the incident


@pytest.fixture
def only_me(monkeypatch):
    monkeypatch.setattr(settings, "bot_allowed_numbers", ME)


# --- production: empty means everyone -------------------------------------------
def test_an_empty_list_lets_everyone_through(monkeypatch):
    """A missing setting must never silently switch the bot off in production."""
    monkeypatch.setattr(settings, "bot_allowed_numbers", "")
    assert allowlist.is_restricted() is False
    assert allowlist.permits(STRANGER) is True


def test_a_list_of_only_commas_and_spaces_is_still_empty(monkeypatch):
    monkeypatch.setattr(settings, "bot_allowed_numbers", " , ,, ")
    assert allowlist.is_restricted() is False


# --- test mode -------------------------------------------------------------------
def test_the_listed_number_is_answered(only_me):
    assert allowlist.permits(ME) is True


def test_a_stranger_is_not(only_me):
    assert allowlist.permits(STRANGER) is False


def test_formatting_does_not_matter(only_me):
    assert allowlist.permits("+91 74012 68091") is True
    assert allowlist.permits("+91-7401-268-091") is True


def test_the_country_code_may_be_left_off_in_the_setting(monkeypatch):
    """People write their own number the way they say it."""
    monkeypatch.setattr(settings, "bot_allowed_numbers", "7401268091")
    assert allowlist.permits("917401268091") is True


def test_a_short_suffix_cannot_let_strangers_through(monkeypatch):
    """"8091" alone would match any number ending in it."""
    monkeypatch.setattr(settings, "bot_allowed_numbers", "8091")
    assert allowlist.permits("917401268091") is False
    assert allowlist.permits("8091") is True


def test_several_numbers_can_be_listed(monkeypatch):
    monkeypatch.setattr(settings, "bot_allowed_numbers", f"{ME}, 14155550123")
    assert allowlist.permits(ME) is True
    assert allowlist.permits("14155550123") is True
    assert allowlist.permits(STRANGER) is False


def test_a_blank_number_is_never_permitted_in_test_mode(only_me):
    assert allowlist.permits("") is False


# --- the webhook -------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_stranger_gets_no_record_and_no_reply(session, only_me, monkeypatch):
    """The whole point: nothing is created and nothing is sent."""
    from sqlalchemy import select

    from app.api.routes import webhooks_gallabox
    from app.db.models import Customer

    handled = []

    async def spy(session, event):
        handled.append(event)
        return True

    monkeypatch.setattr(webhooks_gallabox, "handle_event", spy)
    monkeypatch.setattr(webhooks_gallabox, "verify_gallabox_token", lambda t: True)

    class FakeRequest:
        headers: dict = {}

        async def json(self):
            return {"whatsapp": {"from": STRANGER, "type": "text",
                                 "text": {"body": "where is my order"}}}

    result = await webhooks_gallabox.gallabox_webhook(
        FakeRequest(), session=session, authorization="t",
        x_gallabox_token=None, x_webhook_secret=None, token=None)

    assert result["status"] == "ignored"
    assert handled == []
    stranger = (await session.execute(
        select(Customer).where(Customer.whatsapp_number == STRANGER))).scalar_one_or_none()
    assert stranger is None


@pytest.mark.asyncio
async def test_the_listed_number_reaches_the_bot(session, only_me, monkeypatch):
    from app.api.routes import webhooks_gallabox

    handled = []

    async def spy(session, event):
        handled.append(event)
        return True

    monkeypatch.setattr(webhooks_gallabox, "handle_event", spy)
    monkeypatch.setattr(webhooks_gallabox, "verify_gallabox_token", lambda t: True)

    class FakeRequest:
        headers: dict = {}

        async def json(self):
            return {"whatsapp": {"from": ME, "type": "text", "text": {"body": "hi"}}}

    result = await webhooks_gallabox.gallabox_webhook(
        FakeRequest(), session=session, authorization="t",
        x_gallabox_token=None, x_webhook_secret=None, token=None)

    assert result["status"] == "ok"
    assert len(handled) == 1


# --- outreach ------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_outreach_refuses_a_stranger_in_test_mode(session, only_me):
    from app.integrations.gallabox.sender import use_sender
    from app.services import outreach
    from tests.conversation.test_happy_path import FakeGallabox

    fake = FakeGallabox()
    with use_sender(fake), pytest.raises(outreach.NotAllowlisted):
        await outreach.send_welcome(session, STRANGER)

    assert fake.sent == []
