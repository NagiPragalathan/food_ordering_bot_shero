"""The downloadable PDF bill (services/receipts.py, api/routes/receipts.py)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.core.config import settings
from app.db.models import Order, OrderStage, PaymentStatus
from app.services import receipts

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(settings, "admin_session_secret", "s")
    monkeypatch.setattr(settings, "public_base_url", "https://shero.test")


async def _order(session, customer, outlet, *, paid=True) -> Order:
    order = Order(
        order_number="SHO-260930-BILL1", customer_id=customer.id, outlet_id=outlet.id,
        items=[{"retailer_id": "a", "name": "Drumstick Sambar", "quantity": 2,
                "unit_price": "12.50"},
               {"retailer_id": "b", "name": "Appam " * 30, "quantity": 1,
                "unit_price": "3.25"}],
        dish_total=Decimal("28.25"), delivery_fee=Decimal("5.99"), extra_fees=Decimal("1.10"),
        total=Decimal("35.34"), delivery_address="6360 Lawyers Hill Road",
        apartment_unit="Apt 4", postal_code="21075", contact_number="17325550142",
        slot_label="Wed 30 Sep, 7:00 PM - 8:00 PM", stripe_payment_intent_id="pi_123",
        paid_at=datetime(2026, 9, 30, 13, 5, tzinfo=timezone.utc),
        payment_status=PaymentStatus.PAID if paid else PaymentStatus.LINK_SENT,
        stage=OrderStage.PAID_SLOT_BOOKED if paid else OrderStage.PENDING_PAYMENT)
    session.add(order)
    await session.flush()
    return order


def _client(session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import receipts as route
    from app.db.session import get_session

    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def test_a_token_reads_back_and_a_tampered_one_does_not():
    token = receipts.build_token("SHO-260930-BILL1")
    assert receipts.read_token(token) == "SHO-260930-BILL1"
    assert receipts.read_token(token[:-2] + "xx") is None
    assert receipts.receipt_url("SHO-1").startswith("https://shero.test/receipt/")


async def test_the_bill_is_a_pdf(session, customer, outlet):
    order = await _order(session, customer, outlet)
    body = receipts.build_pdf(order, customer, outlet)
    assert body.startswith(b"%PDF")


async def test_a_paid_order_downloads_its_bill(session, customer, outlet):
    await _order(session, customer, outlet)
    response = _client(session).get(f"/receipt/{receipts.build_token('SHO-260930-BILL1')}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "Shero-bill-SHO-260930-BILL1.pdf" in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF")


async def test_an_unpaid_order_or_a_bad_token_has_no_bill(session, customer, outlet):
    await _order(session, customer, outlet, paid=False)
    client = _client(session)
    assert client.get(f"/receipt/{receipts.build_token('SHO-260930-BILL1')}").status_code == 404
    assert client.get("/receipt/SHO-260930-BILL1").status_code == 404
