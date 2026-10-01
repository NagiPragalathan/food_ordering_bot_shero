"""Approval check before sending a template that has a fallback.

Gallabox accepts a send for an unapproved template, which then fails
silently - so the bot asks first, and a customer is never left with nothing.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone

import pytest

from app.core.exceptions import IntegrationError
from app.integrations.gallabox import template_admin
from app.integrations.gallabox import templates as tpl

# Gallabox's clock in these tests; approvals are stamped relative to it.
NOW = datetime(2026, 9, 28, 7, 0, tzinfo=timezone.utc)
LONG_AGO = "2026-09-24T08:41:31.739Z"
JUST_NOW = "2026-09-28T06:50:00.000Z"      # 10 minutes before NOW


@pytest.fixture
def status(monkeypatch):
    """The real module, not the conftest stub, with Gallabox faked."""
    import app.integrations.gallabox.template_status as module

    real = importlib.reload(module)
    real.clear_cache()
    rows: list[dict] = []
    calls = {"n": 0}

    async def listing():
        calls["n"] += 1
        return rows

    monkeypatch.setattr(template_admin, "list_templates", listing)

    async def clock():
        return NOW

    monkeypatch.setattr(real, "gallabox_now", clock)
    yield real, rows, calls
    real.clear_cache()


async def test_only_an_approved_template_counts(status):
    module, rows, _ = status
    rows += [{"name": "menu_link", "status": "PENDING", "statusUpdatedAt": LONG_AGO},
             {"name": "order_summary", "status": "approved", "statusUpdatedAt": LONG_AGO}]

    assert await module.is_approved("order_summary") is True
    assert await module.is_approved("menu_link") is False
    assert await module.is_approved("never_created") is False


async def test_a_template_approved_minutes_ago_waits_out_gallabox_15_minutes(status):
    """Gallabox refuses a template for its first 15 minutes after approval."""
    module, rows, _ = status
    rows.append({"name": "menu_link", "status": "approved", "statusUpdatedAt": JUST_NOW})

    assert await module.is_approved("menu_link") is False


async def test_without_gallabox_clock_a_template_is_not_trusted(status, monkeypatch):
    module, rows, _ = status
    rows.append({"name": "menu_link", "status": "approved", "statusUpdatedAt": LONG_AGO})

    async def no_clock():
        return None

    monkeypatch.setattr(module, "gallabox_now", no_clock)
    assert await module.is_approved("menu_link") is False


async def test_the_list_is_cached_not_fetched_per_message(status):
    module, rows, calls = status
    rows.append({"name": "menu_link", "status": "approved", "statusUpdatedAt": LONG_AGO})

    for _ in range(5):
        await module.is_approved("menu_link")
    assert calls["n"] == 1


async def test_gallabox_down_means_use_the_fallback(status, monkeypatch):
    module, _, _ = status

    async def down():
        raise IntegrationError("gallabox", "timeout")

    monkeypatch.setattr(template_admin, "list_templates", down)
    assert await module.is_approved("menu_link") is False


# --- the senders honour it ----------------------------------------------------------
def _not_approved(monkeypatch):
    from app.integrations.gallabox import template_status

    async def no(name):
        return False

    monkeypatch.setattr(template_status, "is_approved", no)


async def test_an_unapproved_menu_link_sends_the_plain_link_instead(
        session, outlet, menu, monkeypatch):
    from tests.conversation.test_happy_path import FakeGallabox, _sign_up, \
        _with_signed_links, reply
    from app.integrations.gallabox.sender import use_sender
    from app.services.conversation.engine import handle_event

    _with_signed_links(monkeypatch)
    _not_approved(monkeypatch)
    fake = FakeGallabox()
    with use_sender(fake):
        await _sign_up(session, fake)
        fake.clear()
        await handle_event(session, reply("menu:order"))

    assert fake.kinds() == ["cta_url"]


async def test_an_unapproved_order_summary_sends_payment_link_instead(
        session, customer, monkeypatch):
    from decimal import Decimal

    from app.db.models import Order, OrderStage, PaymentStatus
    from app.integrations.gallabox.sender import use_sender
    from app.services import payments
    from tests.conversation.test_happy_path import FakeGallabox

    _not_approved(monkeypatch)
    order = Order(order_number="SHO-T-2", customer_id=customer.id, items=[],
                  total=Decimal("10.00"), payment_status=PaymentStatus.LINK_SENT,
                  stage=OrderStage.PENDING_PAYMENT)
    session.add(order)
    await session.flush()

    fake = FakeGallabox()
    with use_sender(fake):
        await payments.send_order_summary(order, customer)

    assert fake.last().body == tpl.PAYMENT_LINK.name


async def test_a_template_whose_button_points_at_an_old_address_is_not_used(status, monkeypatch):
    """After the bot moves to a new address, the approved template's button
    still opens the old one; the plain-text fallback carries the right link."""
    module, rows, _ = status
    monkeypatch.setattr(module.settings, "public_base_url", "https://new.example.com")
    monkeypatch.setattr(module.settings, "pay_redirect_base_url", "https://new.example.com/pay")

    def template(name, url):
        return {"name": name, "status": "approved", "statusUpdatedAt": LONG_AGO,
                "components": [{"type": "BUTTONS", "buttons": [
                    {"type": "URL", "text": "Open", "url": url},
                    {"type": "QUICK_REPLY", "text": "Get new link"}]}]}

    rows += [template("menu_link", "https://old.loca.lt/order/{{1}}"),
             template("order_summary", "https://new.example.com/order/{{1}}")]

    assert await module.is_approved("menu_link") is False
    assert await module.is_approved("order_summary") is True
