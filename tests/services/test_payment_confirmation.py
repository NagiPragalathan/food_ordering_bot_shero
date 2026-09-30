"""The "payment received" message (spec step 16) always reaches the customer:
as the payment_success template when approved, as plain text otherwise."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.exceptions import IntegrationError
from app.db.models import Order
from app.services import payments

pytestmark = pytest.mark.asyncio


class Sender:
    def __init__(self, *, template_error: Exception | None = None):
        self.sent: list[tuple] = []
        self.template_error = template_error

    async def send_template(self, to, spec, *values, button_value=None, **_kw):
        if self.template_error:
            raise self.template_error
        self.sent.append(("template", to, spec.name, values, button_value))

    async def send_text(self, to, body, **_kw):
        self.sent.append(("text", to, body))


def _order(customer) -> Order:
    return Order(order_number="SHO-TEST-7", customer_id=customer.id, total=Decimal("33.80"),
                 slot_label="Tue 29 Sep, 11:00 AM - 12:00 PM", items=[])


def _use(monkeypatch, sender, *, approved: bool):
    monkeypatch.setattr(payments, "current_sender", lambda: sender)

    async def is_approved(_name):
        return approved
    monkeypatch.setattr(payments.template_status, "is_approved", is_approved)


async def test_an_approved_template_is_used(customer, monkeypatch):
    sender = Sender()
    _use(monkeypatch, sender, approved=True)

    assert await payments.send_payment_confirmation(customer, _order(customer), "Shero")

    (kind, to, name, values, button), = sender.sent
    assert (kind, to, name) == ("template", customer.whatsapp_number, "payment_confirmed")
    assert values == ("SHO-TEST-7", "33.80", "Shero", "Tue 29 Sep, 11:00 AM - 12:00 PM")
    # The Download Bill button carries the signed receipt token.
    assert payments.receipts.read_token(button) == "SHO-TEST-7"


async def test_without_an_approved_template_the_same_words_go_as_text(customer, monkeypatch):
    sender = Sender()
    _use(monkeypatch, sender, approved=False)

    assert await payments.send_payment_confirmation(customer, _order(customer), "Shero")

    (kind, to, body), = sender.sent
    assert kind == "text" and to == customer.whatsapp_number
    assert body.startswith("Payment received! Your order #SHO-TEST-7 of $33.80 is confirmed "
                           "from Shero for delivery at Tue 29 Sep, 11:00 AM - 12:00 PM. "
                           "Thank you for ordering with Shero!")
    link = body.rsplit("Download your bill: ", 1)[1]
    assert payments.receipts.read_token(link.rsplit("/receipt/", 1)[1]) == "SHO-TEST-7"


async def test_a_refused_template_falls_back_to_text(customer, monkeypatch):
    sender = Sender(template_error=IntegrationError("gallabox", "template rejected"))
    _use(monkeypatch, sender, approved=True)

    assert await payments.send_payment_confirmation(customer, _order(customer), "Shero")
    assert [s[0] for s in sender.sent] == ["text"]
