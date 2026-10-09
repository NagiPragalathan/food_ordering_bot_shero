"""The "payment received" message (spec step 16), with its Download Invoice
button. The not-approved fallback is the client's and is tested in
tests/integrations/test_template_fallback.py."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.exceptions import IntegrationError
from app.db.models import Order
from app.services import payments

pytestmark = pytest.mark.asyncio


class Sender:
    def __init__(self, *, error: Exception | None = None):
        self.sent: list[tuple] = []
        self.error = error

    async def send_template(self, to, spec, *values, button_value=None):
        if self.error:
            raise self.error
        self.sent.append((to, spec.name, values, button_value))


def _order(customer) -> Order:
    return Order(order_number="SHO-TEST-7", customer_id=customer.id, total=Decimal("33.80"),
                 slot_label="Tue 29 Sep, 11:00 AM - 12:00 PM", items=[])


async def test_the_confirmation_carries_the_bill_button(customer, monkeypatch):
    sender = Sender()
    monkeypatch.setattr(payments, "current_sender", lambda: sender)

    assert await payments.send_payment_confirmation(customer, _order(customer), "Shero")

    (to, name, values, button), = sender.sent
    assert (to, name) == (customer.whatsapp_number, "payment_confirmed_v4")
    assert values == ("SHO-TEST-7", "33.80", "Shero", "Tue 29 Sep, 11:00 AM - 12:00 PM")
    assert payments.receipts.read_token(button) == "SHO-TEST-7"


async def test_a_failed_send_is_reported_not_raised(customer, monkeypatch):
    sender = Sender(error=IntegrationError("gallabox", "down"))
    monkeypatch.setattr(payments, "current_sender", lambda: sender)
    assert await payments.send_payment_confirmation(customer, _order(customer), "Shero") is False
