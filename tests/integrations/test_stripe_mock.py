"""Without a Stripe key the checkout is mocked, so ordering still works end to end."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.integrations.stripe_gw import checkout

ITEMS = [{"name": "Sambar", "quantity": 1, "unit_price": "9.50"}]


async def _create():
    return await checkout.create_checkout_session(
        order_id="1", order_number="SHO-260928-ABCDE", items=ITEMS,
        delivery_fee=Decimal("0"), taxes_and_fees=Decimal("0"))


@pytest.mark.asyncio
async def test_no_key_gives_a_mock_session(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    monkeypatch.setattr(settings, "public_base_url", "https://bot.example")

    async def never(**_):
        raise AssertionError("Stripe must not be called in mock mode")
    monkeypatch.setattr(checkout.stripe.checkout.Session, "create_async", never)

    result = await _create()
    assert result["id"] == "mock_SHO-260928-ABCDE"
    assert result["url"] == "https://bot.example/pay/mock/SHO-260928-ABCDE"
    assert result["expires_at"] is not None


@pytest.mark.asyncio
async def test_a_mock_session_is_never_sent_to_stripe_to_expire(monkeypatch):
    async def never(*_):
        raise AssertionError("Stripe must not be called for a mock session")
    monkeypatch.setattr(checkout.stripe.checkout.Session, "expire_async", never)
    await checkout.expire_session("mock_SHO-260928-ABCDE")


@pytest.mark.asyncio
async def test_with_a_key_stripe_is_used(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_123")

    class Session:
        id, url = "cs_1", "https://checkout.stripe.com/x"

    async def create(**_):
        return Session()
    monkeypatch.setattr(checkout.stripe.checkout.Session, "create_async", create)

    assert (await _create())["id"] == "cs_1"


@pytest.mark.asyncio
async def test_the_mock_page_shows_the_amount(session, monkeypatch):
    from app.api.routes import pay
    from app.db.session import get_session

    class Order:
        order_number, total, currency = "SHO-260928-ABCDE", Decimal("9.50"), "USD"
        stripe_session_id, payment_status = "mock_SHO-260928-ABCDE", "link_sent"

    async def by_number(_session, number):
        return Order() if number == Order.order_number else None
    monkeypatch.setattr(pay.order_service, "get_by_number", by_number)

    app = FastAPI()
    app.include_router(pay.router)
    app.dependency_overrides[get_session] = lambda: session
    client = TestClient(app)

    page = client.get("/pay/mock/SHO-260928-ABCDE")
    assert page.status_code == 200
    assert "Test payment" in page.text and "9.50 USD" in page.text
    assert "no card is charged" in page.text
    assert client.get("/pay/mock/SHO-260928-ZZZZZ").status_code == 404


# --- simulating the payment --------------------------------------------------
def _pay_client(session, monkeypatch, order, paid_calls):
    from app.api.routes import pay
    from app.db.session import get_session

    async def by_number(_session, number):
        return order if number == order.order_number else None
    monkeypatch.setattr(pay.order_service, "get_by_number", by_number)

    async def success(_session, the_order, customer, payment_intent_id=None):
        paid_calls.append((the_order.order_number, customer))
    monkeypatch.setattr(pay.payments, "handle_payment_success", success)

    class Session:
        async def get(self, _model, _id):
            return "the-customer"

    app = FastAPI()
    app.include_router(pay.router)
    app.dependency_overrides[get_session] = lambda: Session()
    return TestClient(app)


class _Order:
    order_number, customer_id = "SHO-260928-ABCDE", "c1"
    total, currency, payment_status = Decimal("9.50"), "USD", "link_sent"

    def __init__(self, session_id="mock_SHO-260928-ABCDE"):
        self.stripe_session_id = session_id


def test_simulating_runs_the_same_success_path_as_stripe(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    calls = []
    client = _pay_client(None, monkeypatch, _Order(), calls)
    page = client.post("/pay/mock/SHO-260928-ABCDE/paid")
    assert page.status_code == 200 and "Payment simulated" in page.text
    assert calls == [("SHO-260928-ABCDE", "the-customer")]


def test_with_a_real_key_nothing_can_be_marked_paid_from_here(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_123")
    calls = []
    client = _pay_client(None, monkeypatch, _Order(), calls)
    assert client.post("/pay/mock/SHO-260928-ABCDE/paid").status_code == 404
    assert calls == []


def test_a_real_stripe_order_is_never_paid_from_the_mock_page(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    calls = []
    client = _pay_client(None, monkeypatch, _Order("cs_live_1"), calls)
    assert client.post("/pay/mock/SHO-260928-ABCDE/paid").status_code == 404
    assert calls == []


@pytest.mark.asyncio
async def test_a_wrong_machine_clock_does_not_lose_the_payment_link(monkeypatch):
    """Stripe judges expires_at by its own clock; if it says ours is already
    past, the link is created without one rather than failing the order."""
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_123")
    attempts = []

    class Session:
        id, url = "cs_2", "https://checkout.stripe.com/y"

    async def create(**params):
        attempts.append(params)
        if "expires_at" in params:
            raise checkout.stripe.InvalidRequestError(
                "expires_at expects a timestamp in the future", "expires_at")
        return Session()
    monkeypatch.setattr(checkout.stripe.checkout.Session, "create_async", create)

    assert (await _create())["id"] == "cs_2"
    assert len(attempts) == 2 and "expires_at" not in attempts[1]


@pytest.mark.asyncio
async def test_any_other_stripe_refusal_still_fails_the_link(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_123")

    async def create(**_):
        raise checkout.stripe.InvalidRequestError("bad currency", "currency")
    monkeypatch.setattr(checkout.stripe.checkout.Session, "create_async", create)

    with pytest.raises(checkout.PaymentError):
        await _create()
