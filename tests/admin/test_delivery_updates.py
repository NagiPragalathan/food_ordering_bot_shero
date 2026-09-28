"""Delivery messages to the customer, switched off at the client's request.

The distinction that matters: the order still moves through Sent to Kitchen,
Out for Delivery and Delivered so the kitchen can track it and the feedback
job still has a delivery time to count from. Only the customer's WhatsApp
message is suppressed.
"""

from __future__ import annotations

import pytest

from app.admin.routes.orders import TRANSITIONS, should_notify
from app.core.config import settings
from app.db.models import OrderStage
from app.integrations.gallabox import templates as tpl


def test_no_message_is_sent_while_delivery_updates_are_off(monkeypatch):
    monkeypatch.setattr(settings, "send_delivery_updates", False)
    assert should_notify(tpl.ORDER_OUT_FOR_DELIVERY) is False
    assert should_notify(tpl.ORDER_DELIVERED) is False


def test_messages_resume_when_switched_back_on(monkeypatch):
    monkeypatch.setattr(settings, "send_delivery_updates", True)
    assert should_notify(tpl.ORDER_OUT_FOR_DELIVERY) is True
    assert should_notify(tpl.ORDER_DELIVERED) is True


def test_a_stage_with_no_template_never_messages(monkeypatch):
    """Send to kitchen is internal - it has no customer message either way."""
    monkeypatch.setattr(settings, "send_delivery_updates", True)
    assert should_notify(None) is False


def test_the_stages_themselves_are_untouched_by_the_switch():
    """Both stages still exist, so the kitchen can still track an order."""
    assert TRANSITIONS[OrderStage.PAID_SLOT_BOOKED][1] is OrderStage.SENT_TO_KITCHEN
    assert TRANSITIONS[OrderStage.SENT_TO_KITCHEN][1] is OrderStage.OUT_FOR_DELIVERY
    assert TRANSITIONS[OrderStage.OUT_FOR_DELIVERY][1] is OrderStage.DELIVERED


def test_both_optional_messages_are_off_by_default():
    """What the client asked for, pinned so a refactor cannot switch them on."""
    assert settings.send_payment_reminder is False
    assert settings.send_delivery_updates is False


@pytest.mark.parametrize("spec", [tpl.ORDER_OUT_FOR_DELIVERY, tpl.ORDER_DELIVERED,
                                  tpl.PAYMENT_REMINDER])
def test_the_switched_off_templates_still_exist(spec):
    """They stay approved on the WABA, so turning them back on needs no
    resubmission - only the flag."""
    assert spec.name in tpl.BY_NAME
