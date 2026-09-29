"""The 30-minute "payment expired" message: who gets it, and where Order Now goes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.config import settings
from app.db.models import Order, OrderStage, PaymentStatus
from app.services import cart as cart_service
from app.services import order_changes, orders, payments

pytestmark = pytest.mark.asyncio

LINES = [{"retailer_id": "SAMBAR-1", "name": "Sambar", "quantity": 2,
          "unit_price": "9.50", "line_total": "19.00"}]


async def _unpaid_order(session, customer, *, minutes_ago=40) -> Order:
    created = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    order = Order(order_number="SHO-TEST-7", customer_id=customer.id, items=LINES,
                  item_count=2, dish_total=Decimal("19.00"), total=Decimal("22.00"),
                  stripe_session_id="mock_SHO-TEST-7",
                  checkout_url="https://shero.test/pay/mock/SHO-TEST-7",
                  payment_status=PaymentStatus.LINK_SENT,
                  stage=OrderStage.PENDING_PAYMENT,
                  payment_link_expires_at=created + timedelta(minutes=30))
    session.add(order)
    await session.flush()
    return order


@pytest.fixture
def quiet(monkeypatch):
    """No WhatsApp, Zoho or Stripe calls."""
    sent = []

    class Sender:
        async def send_template(self, to, spec, *values, **kw):
            sent.append(spec.name)

    monkeypatch.setattr(payments, "current_sender", lambda: Sender())

    async def nothing(*_a, **_k):
        return True
    monkeypatch.setattr(payments.crm_sync, "advance_stage", nothing)
    monkeypatch.setattr(order_changes.crm_sync, "advance_stage", nothing)
    monkeypatch.setattr(order_changes.checkout, "expire_session", nothing)
    return sent


async def test_a_lapsed_unpaid_order_is_due(session, customer):
    order = await _unpaid_order(session, customer)
    assert order in await orders.find_expired_orders(session)


async def test_an_order_the_customer_changed_is_never_told_it_expired(
        session, customer, quiet):
    """Update location / Change menu cancels it: no message 30 minutes later."""
    order = await _unpaid_order(session, customer)
    conversation, _ = await cart_service.for_customer(session, customer)
    await order_changes.reopen_latest(session, customer, conversation,
                                      reason="update_location")

    assert order.payment_status == PaymentStatus.EXPIRED
    assert await orders.find_expired_orders(session) == []
    assert await orders.find_orders_for_reminder(session) == []
    # A late Stripe "expired" webhook is ignored too.
    assert await payments.handle_payment_expired(session, order, customer) is False
    assert quiet == []


async def test_a_cancelled_order_is_skipped_even_if_still_marked_link_sent(
        session, customer):
    order = await _unpaid_order(session, customer)
    order.stage = OrderStage.CANCELLED
    await session.flush()
    assert await orders.find_expired_orders(session) == []


async def test_expiry_puts_the_dishes_back_in_the_cart(session, customer, quiet):
    """The message says "your cart is still saved" - a web order emptied it."""
    order = await _unpaid_order(session, customer)
    await payments.handle_payment_expired(session, order, customer)

    _, lines = await cart_service.for_customer(session, customer)
    assert [(l["retailer_id"], l["quantity"]) for l in lines] == [("SAMBAR-1", 2)]
    assert quiet == ["payment_expired"]


async def test_expiry_keeps_a_cart_the_customer_has_started_since(session, customer, quiet):
    order = await _unpaid_order(session, customer)
    conversation, _ = await cart_service.for_customer(session, customer)
    conversation.set(cart=[{"retailer_id": "DOSA-1", "name": "Dosa", "quantity": 1,
                            "unit_price": "8.00"}])
    await payments.handle_payment_expired(session, order, customer)

    _, lines = await cart_service.for_customer(session, customer)
    assert [l["retailer_id"] for l in lines] == ["DOSA-1"]


async def test_order_now_on_an_expired_order_opens_the_menu(session, customer, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import pay
    from app.db.session import get_session

    monkeypatch.setattr(settings, "admin_session_secret", "s")
    monkeypatch.setattr(settings, "public_base_url", "https://shero.test")
    order = await _unpaid_order(session, customer)
    order.order_number = "SHO-260927-ERXSC"
    order.payment_status = PaymentStatus.EXPIRED
    await session.flush()

    app = FastAPI()
    app.include_router(pay.router)
    app.dependency_overrides[get_session] = lambda: session
    response = TestClient(app).get("/pay/SHO-260927-ERXSC", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"].startswith("https://shero.test/order/")


async def test_pay_now_redirects_to_stripe_for_a_live_link(session, customer, monkeypatch):
    """SQLite gives the expiry stamp back without a timezone; the redirect
    once crashed comparing it with an aware "now" (a 500 on every Pay Now)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import pay
    from app.db.session import get_session

    monkeypatch.setattr(settings, "admin_session_secret", "s")
    order = await _unpaid_order(session, customer, minutes_ago=5)
    order.order_number = "SHO-260929-LIVE1"
    order.checkout_url = "https://checkout.stripe.com/c/pay/cs_test_live"
    await session.flush()
    await session.refresh(order)              # reload exactly as SQLite stores it
    assert order.payment_link_expires_at.tzinfo is None

    app = FastAPI()
    app.include_router(pay.router)
    app.dependency_overrides[get_session] = lambda: session
    response = TestClient(app).get("/pay/SHO-260929-LIVE1", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "https://checkout.stripe.com/c/pay/cs_test_live"
