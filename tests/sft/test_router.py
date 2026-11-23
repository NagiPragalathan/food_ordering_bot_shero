"""Which WhatsApp messages are SFT's, and that the ordering bot keeps the rest."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.sft import config, leads, router
from app.sft import content as c
from tests.sft.conftest import NUMBER, event

SFT_CHANNEL = "6b0a1c2d3e4f5a6b7c8d9e0f"
US_CHANNEL = "6a2fee09a806dc48e9e4eef7"


async def test_off_takes_nothing(session, rec):
    await config.save(session, {"mode": "off"}, "t")
    assert await router.route(session, event("SFT")) is None
    assert rec.sent == [] and await leads.get(session, NUMBER) is None


async def test_nothing_saved_means_off(session, rec):
    assert await router.route(session, event("SFT")) is None


async def test_on_the_shared_number_only_sft_messages_are_taken(session, cfg, rec):
    assert await router.route(session, event("hi")) is None              # the ordering bot's
    assert await router.route(session, event("order food")) is None
    taken = await router.route(session, event("SFT"))
    assert taken["status"] == "ok" and "sft" in taken["reason"]


async def test_the_name_and_question_are_taken_without_the_keyword(session, cfg, rec):
    await router.route(session, event("SFT"))
    assert (await router.route(session, event("Priya")))["status"] == "ok"
    lead = await leads.get(session, NUMBER)
    assert lead.name == "Priya"


async def test_an_sft_button_is_taken(session, cfg, rec):
    await router.route(session, event("SFT"))
    await router.route(session, event("Priya"))
    taken = await router.route(session, event(button=c.B_SKILLS, title="Improve my skills"))
    assert taken["status"] == "ok"


async def test_a_follow_up_quick_reply_is_taken_for_a_lead(session, cfg, rec):
    await router.route(session, event("SFT"))
    assert (await router.route(session, event("Enroll now")))["status"] == "ok"
    # ...but not from someone SFT has never talked to.
    assert await router.route(session, event("Enroll now", number="15559990000")) is None


async def test_a_lead_gone_quiet_is_the_ordering_bots_again(session, cfg, rec):
    await router.route(session, event("SFT"))
    lead = await leads.get(session, NUMBER)
    lead.last_outbound_at = leads.now() - timedelta(hours=router.TYPING_HOURS + 1)
    assert await router.route(session, event("Priya")) is None


async def test_test_mode_takes_only_the_test_numbers(session, rec):
    await config.save(session, {"mode": "test", "test_numbers": "15551230001"}, "t")
    assert (await router.route(session, event("SFT")))["status"] == "ok"
    assert await router.route(session, event("SFT", number="15557778888")) is None


async def test_an_sft_ad_starts_the_journey(session, rec):
    await config.save(session, {"mode": "live", "ad_ids": "1202"}, "t")
    taken = await router.route(session, event("Hi!", ad_id="1202", referral={"source_id": "1202"}))
    assert taken["status"] == "ok"


async def test_with_the_team_the_lead_is_not_handed_to_the_ordering_bot(session, cfg, rec):
    await router.route(session, event("SFT"))
    await router.route(session, event("Priya"))
    await router.route(session, event("Can I pay in two parts?"))
    rec.clear()
    taken = await router.route(session, event("hello"))
    assert taken == {"status": "ok", "reason": "sft: with team"} and rec.sent == []


# --- SFT's own number ---------------------------------------------------------------------
@pytest.fixture
def numbers(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "gallabox_channel_id", US_CHANNEL)


async def test_its_own_number_takes_every_message(session, numbers, rec):
    cfg = await config.save(session, {"mode": "live", "channel_id": SFT_CHANNEL}, "t")
    taken = await router.route(session, event("hello", channel_id=SFT_CHANNEL))
    assert taken["status"] == "ok"
    lead = await leads.get(session, NUMBER)
    assert lead.channel_id == SFT_CHANNEL and cfg.channel_id == SFT_CHANNEL


async def test_with_its_own_number_the_shared_one_is_left_alone(session, numbers, rec):
    await config.save(session, {"mode": "live", "channel_id": SFT_CHANNEL}, "t")
    assert await router.route(session, event("SFT", channel_id=US_CHANNEL)) is None


async def test_its_own_number_in_test_mode_ignores_strangers(session, numbers, rec):
    await config.save(session, {"mode": "test", "channel_id": SFT_CHANNEL,
                                "test_numbers": "15550000009"}, "t")
    taken = await router.route(session, event("hello", channel_id=SFT_CHANNEL))
    assert taken == {"status": "ignored", "reason": "sft test mode"} and rec.sent == []


async def test_replies_go_out_on_sfts_own_channel(session, numbers, monkeypatch):
    from app.integrations.gallabox import client

    seen = []

    class Spy:
        async def send_text(self, to, body, name=None):
            seen.append(client.channel_id())

    from app.integrations.gallabox.sender import use_sender

    await config.save(session, {"mode": "live", "channel_id": SFT_CHANNEL}, "t")
    with use_sender(Spy()):
        await router.route(session, event("hello", channel_id=SFT_CHANNEL))
    assert seen == [SFT_CHANNEL]
    assert client.channel_id() == US_CHANNEL                 # back to Shero US's afterwards


# --- through the webhook -----------------------------------------------------------------
async def _webhook(session, monkeypatch, text: str, number: str = NUMBER) -> tuple[dict, list]:
    from app.api.routes import webhooks_gallabox

    handled = []

    async def food_bot(session, event, **_):
        handled.append(event)
        return True

    monkeypatch.setattr(webhooks_gallabox, "handle_event", food_bot)
    monkeypatch.setattr(webhooks_gallabox, "verify_gallabox_token", lambda t: True)
    body = {"id": "w1", "contact": {"name": "Asha"},
            "whatsapp": {"from": number, "type": "text", "text": {"body": text}}}

    class FakeRequest:
        headers: dict = {}

        async def json(self):
            return body

    result = await webhooks_gallabox.gallabox_webhook(
        FakeRequest(), session=session, authorization="t", x_gallabox_token=None,
        x_webhook_secret=None, token=None)
    return result, handled


async def test_the_webhook_gives_sft_its_messages_and_the_bot_the_rest(session, cfg, rec,
                                                                    monkeypatch):
    result, food = await _webhook(session, monkeypatch, "SFT")
    assert result["status"] == "ok" and "sft" in result["reason"] and food == []
    # Another customer ordering food: the ordering bot's, as always.
    result, food = await _webhook(session, monkeypatch, "I want to order lunch",
                                  number="15559876543")
    assert len(food) == 1


async def test_a_food_keyword_mid_journey_still_reaches_the_ordering_bot(session, cfg, rec,
                                                                       monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "bot_reply_trigger", "keywords")
    monkeypatch.setattr(settings, "bot_trigger_keywords",
                        '[{"keyword": "order food", "match": "contains"}]')
    await router.route(session, event("SFT"))
    await router.route(session, event("Priya"))
    assert await router.route(session, event("I want to order food")) is None
    assert (await router.route(session, event("what does it cost?")))["status"] == "ok"


async def test_the_webhook_is_unchanged_while_sft_is_off(session, rec, monkeypatch):
    result, food = await _webhook(session, monkeypatch, "SFT")
    assert len(food) == 1 and rec.sent == []
