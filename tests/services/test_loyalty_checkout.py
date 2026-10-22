"""Public quote/confirm, payment and refund integration for loyalty."""
from decimal import Decimal
import pytest
from sqlalchemy import select
from app.db.models import Order, PaymentStatus, LoyaltyEntry
from app.services import loyalty, payments
from app.api.routes import order_web, webhooks_stripe
from tests.services.test_loyalty import configure, grant, balance
from tests.services.test_promotions import web

async def test_web_points_total_is_reserved_charged_and_earned(session, web, customer, monkeypatch):
    await configure(session)
    await grant(session, customer)
    quote = await web.call("quote", {**web.body, "loyalty_points":200})
    assert quote["ok"] and quote["loyalty"]["discount"] == "2.00"
    assert quote["total"] == pytest.approx(30.99)
    captured = {}
    async def checkout(**kwargs):
        captured.update(kwargs)
        from datetime import datetime, timedelta, timezone
        return {"id":"cs_loyalty", "url":"https://stripe.test/pay", "expires_at":datetime.now(timezone.utc)+timedelta(minutes=30)}
    monkeypatch.setattr(payments.checkout, "create_checkout_session", checkout)
    result = await web.call("confirm", {**web.body, "loyalty_points":200,
                      "loyalty_discount":"2.00", "expected_total":"30.99"})
    assert result["ok"], result
    assert captured["discount"] == Decimal("2")
    order = (await session.execute(select(Order).where(Order.order_number == result["order_number"]))).scalar_one()
    assert order.total == Decimal("30.99") and order.loyalty_state == "held"
    assert await balance(session, customer) == 800
    async def nothing(*args, **kwargs):
        return True
    monkeypatch.setattr(payments, "send_payment_confirmation", nothing)
    monkeypatch.setattr(payments.crm_sync, "convert_and_record_order", nothing)
    await payments.handle_payment_success(session, order, customer)
    assert order.loyalty_earned == 48  # $23 net eligible food + 25 first bonus
    assert await balance(session, customer) == 848
    await payments.handle_payment_success(session, order, customer)
    assert await balance(session, customer) == 848

async def test_confirm_requires_requote_when_loyalty_value_changes(session, web, customer):
    await configure(session)
    await grant(session, customer)
    result = await web.call("confirm", {**web.body,"loyalty_points":200,
                            "loyalty_discount":"1.00", "expected_total":"31.99"})
    assert not result["ok"] and result["requote"]
    assert (await session.execute(select(Order))).scalar_one_or_none() is None
    assert await balance(session, customer) == 1000

async def test_failed_payment_link_releases_points(session, web, customer, monkeypatch):
    from app.core.exceptions import PaymentError
    await configure(session)
    await grant(session, customer)
    async def fail(*args, **kwargs):
        raise PaymentError("Stripe unavailable")
    monkeypatch.setattr(payments, "create_payment_link", fail)
    result = await web.call("confirm", {**web.body,"loyalty_points":200,
                             "loyalty_discount":"2.00", "expected_total":"30.99"})
    assert not result["ok"]
    assert await balance(session, customer) == 1000
    order = (await session.execute(select(Order))).scalar_one()
    assert order.loyalty_state == "released"

async def test_expired_link_releases_points(session, web, customer):
    await configure(session)
    await grant(session, customer)
    result = await web.call("confirm", {**web.body,"loyalty_points":200,
                            "loyalty_discount":"2.00", "expected_total":"30.99"})
    assert result["ok"]
    order = (await session.execute(select(Order))).scalar_one()
    await payments.handle_payment_expired(session, order, customer)
    assert await balance(session, customer) == 1000
    assert order.loyalty_earned == 0

async def test_wallet_requires_customer_token_and_hides_admin_notes(session, web, customer):
    await configure(session)
    await grant(session, customer)
    data = await web.call("loyalty_json")
    assert data["balance"] == 1000 and len(data["entries"]) == 1
    assert "note" not in data["entries"][0] and "actor" not in data["entries"][0]
    denied = await order_web.loyalty_json("invalid-token", session=session)
    assert not denied["ok"]

async def test_external_refund_cumulative_events_reverse_rewards(session, customer, outlet):
    from tests.services.test_loyalty import draft, pay
    await configure(session, first_order_bonus=0)
    await grant(session, customer)
    order = await draft(session, customer, outlet, 200)
    await pay(session, order)
    await webhooks_stripe._record_dashboard_refund(session, order, {"amount_refunded":4900})
    assert await balance(session, customer) == 949
    await webhooks_stripe._record_dashboard_refund(session, order, {"amount_refunded":9800})
    await webhooks_stripe._record_dashboard_refund(session, order, {"amount_refunded":4900})
    assert await balance(session, customer) == 1000
    assert order.loyalty_state == "refunded"


async def test_chat_slot_race_releases_reserved_points(session, web, customer, outlet, monkeypatch):
    from app.core.exceptions import NoSlotsAvailableError
    from app.services.conversation.context import FlowContext
    from app.services.conversation.handlers import checkout
    from app.services import cart
    from app.schemas.inbound import InboundEvent
    await configure(session)
    await grant(session, customer)
    conversation, _ = await cart.for_customer(session, customer)
    conversation.set(loyalty_points=200, slot_id=web.body["slot_id"], outlet_id=str(outlet.id))
    ctx = FlowContext(session=session, customer=customer, conversation=conversation,
                      event=InboundEvent(whatsapp_number=customer.whatsapp_number))
    async def full(*args, **kwargs):
        raise NoSlotsAvailableError("full")
    monkeypatch.setattr(checkout.slots, "hold_slot", full)
    with pytest.raises(NoSlotsAvailableError):
        await checkout.build_and_show_summary(ctx)
    assert await balance(session, customer) == 1000
    order = (await session.execute(select(Order))).scalar_one()
    assert order.loyalty_state == "released"


async def test_chat_failed_payment_creation_releases_points(session, customer, outlet, monkeypatch):
    from app.core.exceptions import PaymentError
    from app.services.conversation.context import FlowContext
    from app.services.conversation.handlers import checkout
    from app.services import cart
    from app.schemas.inbound import InboundEvent
    from app.integrations.gallabox.sender import use_sender
    from tests.services.test_loyalty import draft
    await configure(session)
    await grant(session, customer)
    order = await draft(session, customer, outlet, 200)
    conversation, _ = await cart.for_customer(session, customer)
    ctx = FlowContext(session=session, customer=customer, conversation=conversation,
                      event=InboundEvent(whatsapp_number=customer.whatsapp_number))
    class Sender:
        async def send_text(self, *args, **kwargs): pass
    async def fail(*args, **kwargs):
        raise PaymentError("no checkout session")
    monkeypatch.setattr(payments, "create_payment_link", fail)
    with use_sender(Sender()), pytest.raises(PaymentError):
        await checkout._send_payment(ctx, order)
    assert await balance(session, customer) == 1000
    assert order.loyalty_state == "released"
