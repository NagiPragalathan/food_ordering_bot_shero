"""The web order summary on WhatsApp, and its Change menu / Update location buttons.

After a web order the summary goes to WhatsApp as the order_summary
template (Pay Now, Change menu, Update location). The two quick replies
release the unpaid order - slot hold and Stripe link - and put its dishes
back in the cart before sending a fresh menu link.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.exceptions import IntegrationError
from app.db.models import Order, OrderStage, PaymentStatus
from app.integrations.gallabox import templates as tpl
from app.integrations.stripe_gw import checkout
from app.services import order_changes, order_link, payments
from app.services.conversation.engine import handle_event
from app.services.customers import get_or_create_conversation
from tests.conversation.test_happy_path import (  # noqa: F401 - `bot` is a fixture
    DRUMSTICK,
    _event,
    _sign_up,
    _state,
    _with_signed_links,
    bot,
)
from app.schemas.inbound import InboundKind

LINES = [{"retailer_id": DRUMSTICK, "name": "Drumstick Sambar", "quantity": 2,
          "unit_price": "12.50", "line_total": "25.00"}]


def template_button(label: str):
    """A template quick reply arrives as a button reply carrying its label."""
    return _event(kind=InboundKind.REPLY, reply_id=label, reply_title=label)


async def _pending_order(session, customer, *, paid=False, stripe_id="cs_test_1") -> Order:
    order = Order(
        order_number="SHO-TEST-1", customer_id=customer.id, items=LINES, item_count=2,
        dish_total=Decimal("25.00"), total=Decimal("29.42"),
        delivery_address="12 Maple Street", apartment_unit="2B", postal_code="08820",
        slot_label="Fri 25 Sep, 7:00 PM - 8:00 PM",
        stripe_session_id=stripe_id,
        payment_status=PaymentStatus.PAID if paid else PaymentStatus.LINK_SENT,
        stage=OrderStage.PAID_SLOT_BOOKED if paid else OrderStage.PENDING_PAYMENT,
    )
    session.add(order)
    await session.flush()
    return order


def _no_stripe(monkeypatch) -> list[str]:
    expired: list[str] = []

    async def expire(session_id):
        expired.append(session_id)

    monkeypatch.setattr(checkout, "expire_session", expire)
    return expired


# --- the summary itself --------------------------------------------------------------
def test_items_are_one_line_and_capped():
    assert payments.summary_items(LINES) == "2 x Drumstick Sambar"
    many = [{"name": "X" * 150, "quantity": 1}] * 5
    text = payments.summary_items(many)
    assert "\n" not in text
    assert text.endswith("more")
    assert len(text) <= payments.SUMMARY_ITEMS_LIMIT + 20


async def test_the_summary_goes_out_as_the_order_summary_template(session, customer, bot):
    order = await _pending_order(session, customer)

    await payments.send_order_summary(order, customer)

    sent = bot.last()
    assert sent.body == tpl.ORDER_SUMMARY.name
    assert sent.payload["values"] == [
        "Asha Menon", "SHO-TEST-1", "2 x Drumstick Sambar",
        "12 Maple Street, 2B, 08820", "Fri 25 Sep, 7:00 PM - 8:00 PM", "29.42"]
    assert sent.payload["button_value"] == "SHO-TEST-1"


async def test_until_meta_approves_it_the_payment_link_is_sent_instead(
        session, customer, bot, monkeypatch):
    """Never leave a customer with an order and no way to pay."""
    order = await _pending_order(session, customer)
    real = bot.send_template

    async def not_approved_yet(to, spec, *values, button_value=None):
        if spec is tpl.ORDER_SUMMARY:
            raise IntegrationError("gallabox", "template not approved")
        await real(to, spec, *values, button_value=button_value)

    monkeypatch.setattr(bot, "send_template", not_approved_yet)
    await payments.send_order_summary(order, customer)

    assert bot.last().body == tpl.PAYMENT_LINK.name


# --- releasing the order ---------------------------------------------------------------
async def test_reopening_releases_the_order_and_restores_the_cart(
        session, customer, monkeypatch):
    expired = _no_stripe(monkeypatch)
    order = await _pending_order(session, customer)
    conversation = await get_or_create_conversation(session, customer)

    result = await order_changes.reopen_latest(session, customer, conversation,
                                               reason="change_menu")

    assert result.order_number == "SHO-TEST-1"
    assert order.stage == OrderStage.CANCELLED
    assert expired == ["cs_test_1"]                  # the old Pay Now link is dead
    assert conversation.get("cart") == [{
        "retailer_id": DRUMSTICK, "name": "Drumstick Sambar", "quantity": 2,
        "unit_price": "12.50"}]


async def test_a_paid_order_is_never_released(session, customer, monkeypatch):
    expired = _no_stripe(monkeypatch)
    order = await _pending_order(session, customer, paid=True)
    conversation = await get_or_create_conversation(session, customer)

    result = await order_changes.reopen_latest(session, customer, conversation,
                                               reason="change_menu")

    assert result.already_paid is True
    assert order.stage == OrderStage.PAID_SLOT_BOOKED
    assert expired == []


async def test_nothing_pending_is_not_an_error(session, customer):
    conversation = await get_or_create_conversation(session, customer)
    result = await order_changes.reopen_latest(session, customer, conversation,
                                               reason="change_menu")
    assert result == order_changes.Reopened()


# --- the buttons in the chat ------------------------------------------------------------
async def test_change_menu_sends_a_fresh_link_to_the_saved_cart(
        session, outlet, menu, bot, monkeypatch):
    _with_signed_links(monkeypatch)
    _no_stripe(monkeypatch)
    await _sign_up(session, bot)
    customer, _ = await _state(session)
    await _pending_order(session, customer)
    bot.clear()

    await handle_event(session, template_button("Change menu"))

    link = bot.sent[0]
    assert link.kind == "cta_url"
    assert "SHO-TEST-1" in link.body and "kept your cart" in link.body
    assert "?open=" not in link.payload["url"]


async def test_update_location_opens_the_address_only_page(
        session, outlet, menu, bot, monkeypatch):
    _with_signed_links(monkeypatch)
    _no_stripe(monkeypatch)
    await _sign_up(session, bot)
    customer, _ = await _state(session)
    await _pending_order(session, customer)
    bot.clear()

    await handle_event(session, template_button("Update location"))

    link = bot.sent[0]
    assert link.payload["url"].endswith("/location")
    assert link.payload["label"] == "Update address"
    assert "Update address" in link.body


async def test_a_paid_order_gets_pointed_at_a_human(session, outlet, menu, bot, monkeypatch):
    _with_signed_links(monkeypatch)
    await _sign_up(session, bot)
    customer, _ = await _state(session)
    await _pending_order(session, customer, paid=True)
    bot.clear()

    await handle_event(session, template_button("Change menu"))

    assert "already paid" in bot.last().body


def test_only_known_steps_reach_the_link(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "admin_session_secret", "s")
    monkeypatch.setattr(settings, "public_base_url", "https://shero.test")
    assert order_link.build_url("00000000-0000-0000-0000-000000000001",
                                open_at="address").endswith("/location")
    odd = order_link.build_url("00000000-0000-0000-0000-000000000001",
                               open_at="javascript:alert(1)")
    assert "javascript" not in odd and not odd.endswith("/location")
