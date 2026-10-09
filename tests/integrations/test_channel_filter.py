"""The bot answers only its own Gallabox channel (integrations/gallabox/channel.py).

Gallabox delivered every WhatsApp number in the account to the bot's webhook.
The bot greeted other teams' customers, asked their names ("I want to join",
"Thank you") and wrote them into Zoho. The numbers below are the shapes seen
in the real webhook bodies.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.db.models import Customer
from app.integrations.gallabox import channel
from app.schemas.inbound import parse_inbound

OURS_ID, OURS_NUMBER = "6a2fee09a806dc48e9e4eef7", "14438011011"
OTHER_ID, OTHER_NUMBER = "659f88031fb2db4974d16ee8", "918690666666"


def _body(channel_id: str | None, channel_number: str | None, text: str = "hi") -> dict:
    body = {"accountId": "acc", "channelType": "whatsapp", "id": "m1",
            "contact": {"name": "Asha"},
            "whatsapp": {"from": "918925832867", "type": "text", "text": {"body": text}}}
    if channel_id is not None:
        body["channelId"] = channel_id
    if channel_number is not None:
        body["channelNumber"] = channel_number
    return body


@pytest.fixture
def our_channel(monkeypatch):
    monkeypatch.setattr(settings, "gallabox_channel_id", OURS_ID)
    monkeypatch.setattr(settings, "whatsapp_business_number", "+1 443-801-1011")


def test_the_channel_is_read_from_the_webhook():
    event = parse_inbound(_body(OTHER_ID, OTHER_NUMBER))
    assert (event.channel_id, event.channel_number) == (OTHER_ID, OTHER_NUMBER)


def test_our_channel_is_answered(our_channel):
    assert channel.is_ours(parse_inbound(_body(OURS_ID, OURS_NUMBER)))


def test_another_teams_channel_is_not(our_channel):
    assert not channel.is_ours(parse_inbound(_body(OTHER_ID, OTHER_NUMBER)))


def test_the_id_decides_over_the_number(our_channel):
    assert not channel.is_ours(parse_inbound(_body(OTHER_ID, OURS_NUMBER)))


def test_the_number_is_the_fallback(our_channel, monkeypatch):
    monkeypatch.setattr(settings, "gallabox_channel_id", "")
    assert channel.is_ours(parse_inbound(_body(None, OURS_NUMBER)))
    assert not channel.is_ours(parse_inbound(_body(None, OTHER_NUMBER)))


def test_a_message_from_no_known_channel_is_refused(our_channel):
    """Answering a stranger is worse than missing a message."""
    assert not channel.is_ours(parse_inbound(_body(None, None)))


def test_with_nothing_configured_everything_counts():
    assert channel.is_ours(parse_inbound(_body(OTHER_ID, OTHER_NUMBER)))


async def _webhook(session, monkeypatch, body: dict) -> tuple[dict, list]:
    from app.api.routes import webhooks_gallabox

    handled = []

    async def spy(session, event, **_):
        handled.append(event)
        return True

    monkeypatch.setattr(webhooks_gallabox, "handle_event", spy)
    monkeypatch.setattr(webhooks_gallabox, "verify_gallabox_token", lambda t: True)

    class FakeRequest:
        headers: dict = {}

        async def json(self):
            return body

    result = await webhooks_gallabox.gallabox_webhook(
        FakeRequest(), session=session, authorization="t",
        x_gallabox_token=None, x_webhook_secret=None, token=None)
    return result, handled


async def test_the_webhook_drops_another_channel_before_anything_else(session, our_channel,
                                                                     monkeypatch):
    result, handled = await _webhook(session, monkeypatch, _body(OTHER_ID, OTHER_NUMBER))
    assert result == {"status": "ignored", "reason": "another channel"} and handled == []
    assert (await session.execute(select(func.count(Customer.id)))).scalar_one() == 0


async def test_the_webhook_passes_our_channel_on(session, our_channel, monkeypatch):
    result, handled = await _webhook(session, monkeypatch, _body(OURS_ID, OURS_NUMBER))
    assert result["status"] == "ok" and len(handled) == 1


# --- the report of who was recorded from other channels -------------------------------
async def test_the_report_lists_only_people_never_seen_on_our_channel(session, our_channel):
    from app.db.models import InboundMessage
    from app.services import channel_report

    def logged(number: str, channel_id: str, channel_number: str, n: int) -> InboundMessage:
        body = _body(channel_id, channel_number)
        body["whatsapp"]["from"] = number
        return InboundMessage(provider_message_id=f"{number}-{n}", whatsapp_number=number,
                              payload=body)

    stranger = Customer(whatsapp_number="919676837136", name="Kitchen off cheyandi")
    both = Customer(whatsapp_number="918925832867", name="Maniraj")
    session.add_all([stranger, both,
                     logged("919676837136", OTHER_ID, OTHER_NUMBER, 1),
                     logged("919676837136", OTHER_ID, OTHER_NUMBER, 2),
                     logged("918925832867", OTHER_ID, OTHER_NUMBER, 1),
                     logged("918925832867", OURS_ID, OURS_NUMBER, 2)])
    await session.flush()

    report = await channel_report.build(session)

    assert [f.customer.name for f in report.customers] == ["Kitchen off cheyandi"]
    assert report.customers[0].messages == 2 and report.customers[0].channels == {OTHER_NUMBER}
    assert report.other_messages == 3
