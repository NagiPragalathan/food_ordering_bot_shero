"""A template that is not approved goes as an ordinary message instead
(gallabox/fallback.py, GallaboxClient.send_template)."""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.core.exceptions import IntegrationError
from app.integrations.gallabox import template_status
from app.integrations.gallabox import templates as tpl
from app.integrations.gallabox.client import GallaboxClient

pytestmark = pytest.mark.asyncio


class RecordingClient(GallaboxClient):
    """The real send_template, with the network send recorded instead."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[dict] = []
        self.fail_first = False

    async def _send(self, to: str, whatsapp_block: dict, *, name: str | None = None) -> dict:
        payload = whatsapp_block
        if self.fail_first and not self.sent:
            self.sent.append({"failed": payload})
            raise IntegrationError("gallabox", "template refused")
        self.sent.append(payload)
        return {}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://shero.test")
    monkeypatch.setattr(settings, "pay_redirect_base_url", "https://shero.test/pay")
    return RecordingClient()


def _approved(monkeypatch, value: bool):
    async def is_approved(_name):
        return value
    monkeypatch.setattr(template_status, "is_approved", is_approved)


async def test_an_approved_template_is_sent_as_a_template(client, monkeypatch):
    _approved(monkeypatch, True)
    await client.send_template("1555", tpl.ORDER_DELIVERED, "SHO-1")
    (payload,), = [client.sent]
    assert payload["type"] == "template"


async def test_without_approval_the_body_goes_as_text(client, monkeypatch):
    _approved(monkeypatch, False)
    await client.send_template("1555", tpl.ORDER_DELIVERED, "SHO-1")
    (payload,), = [client.sent]
    assert payload["type"] == "text"
    assert "SHO-1" in payload["text"]["body"] and "{{" not in payload["text"]["body"]


async def test_a_url_button_becomes_a_link_button_to_the_same_place(client, monkeypatch):
    _approved(monkeypatch, False)
    await client.send_template("1555", tpl.PAYMENT_SUCCESS, "SHO-1", "33.80", "Shero",
                               "Tue 7 PM", button_value="TOKEN")
    (payload,), = [client.sent]
    action = payload["interactive"]["action"]["parameters"]
    assert action["url"] == "https://shero.test/receipt/TOKEN"
    assert action["display_text"] == "Download Bill"
    assert "Payment received! Your order #SHO-1 of $33.80" in payload["interactive"]["body"]["text"]


async def test_pay_now_points_at_the_short_payment_redirect(client, monkeypatch):
    _approved(monkeypatch, False)
    await client.send_template("1555", tpl.PAYMENT_LINK, "Asha", "SHO-1", "33.80", "Tue 7 PM",
                               button_value="SHO-1")
    (payload,), = [client.sent]
    assert payload["interactive"]["action"]["parameters"]["url"] == "https://shero.test/pay/SHO-1"


async def test_quick_replies_become_buttons_that_answer_the_same_way(client, monkeypatch):
    _approved(monkeypatch, False)
    await client.send_template("1555", tpl.FEEDBACK_REQUEST, "SHO-1")
    (payload,), = [client.sent]
    buttons = payload["interactive"]["action"]["buttons"]
    # Id = label: what a template quick-reply tap sends back.
    assert [(b["reply"]["id"], b["reply"]["title"]) for b in buttons] == [
        ("Great", "Great"), ("Good", "Good"), ("Poor", "Poor")]


async def test_a_template_gallabox_refuses_falls_back_too(client, monkeypatch):
    _approved(monkeypatch, True)
    client.fail_first = True
    await client.send_template("1555", tpl.ORDER_DELIVERED, "SHO-1")
    assert client.sent[0]["failed"]["type"] == "template"
    assert client.sent[1]["type"] == "text"


async def test_wrong_values_are_still_refused_before_anything_is_sent(client, monkeypatch):
    _approved(monkeypatch, False)
    with pytest.raises(ValueError):
        await client.send_template("1555", tpl.ORDER_DELIVERED)
    assert client.sent == []
