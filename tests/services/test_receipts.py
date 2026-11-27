"""The PDF invoice, only once the order is delivered (services/receipts.py,
api/routes/receipts.py)."""

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


async def _order(session, customer, outlet, *, paid=True, delivered=True) -> Order:
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
        stage=(OrderStage.DELIVERED if paid and delivered else
               OrderStage.SENT_TO_KITCHEN if paid else OrderStage.PENDING_PAYMENT),
        delivered_at=datetime(2026, 9, 30, 19, 20, tzinfo=timezone.utc)
        if paid and delivered else None)
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


async def test_a_delivered_order_downloads_its_invoice(session, customer, outlet):
    await _order(session, customer, outlet)
    response = _client(session).get(f"/receipt/{receipts.build_token('SHO-260930-BILL1')}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "Shero-invoice-SHO-260930-BILL1.pdf" in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF")


async def test_an_unpaid_order_or_a_bad_token_has_no_bill(session, customer, outlet):
    await _order(session, customer, outlet, paid=False)
    client = _client(session)
    assert client.get(f"/receipt/{receipts.build_token('SHO-260930-BILL1')}").status_code == 404
    assert client.get("/receipt/SHO-260930-BILL1").status_code == 404


async def test_before_delivery_there_is_no_invoice_yet(session, customer, outlet):
    """The client: invoices only after delivery, not on placing the order. A
    link from an older payment confirmation says when it comes."""
    order = await _order(session, customer, outlet, delivered=False)
    assert not receipts.invoiced(order)
    response = _client(session).get(f"/receipt/{receipts.build_token('SHO-260930-BILL1')}")
    assert response.status_code == 404
    assert "as soon as it is delivered" in response.text
    assert not response.content.startswith(b"%PDF")
    order.stage, order.delivered_at = OrderStage.DELIVERED, datetime.now(timezone.utc)
    await session.flush()
    assert receipts.invoiced(order)


async def test_a_long_kitchen_name_and_address_keep_to_their_column(session, customer, outlet):
    """India's kitchens have long names and addresses: they used to run over
    PAID INVOICE and the order number. Times are the kitchen's own."""
    from zoneinfo import ZoneInfo

    from fpdf import FPDF

    order = await _order(session, customer, outlet)
    outlet.name = "Shero Home Food-Chettinad SKID-IND-291 A Extra Long Kitchen Name"
    outlet.address_line1 = ("House No: 3, Periandavar street, S.V.Nagar, Oragadam, Ambattur, "
                            "Chennai - 600053, Ambathur Mpty Ward-79, Ambattur Taluk " * 2)
    assert receipts.build_pdf(order, customer, outlet).startswith(b"%PDF")
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "", 8.5)
    lines = receipts._wrap(pdf, receipts._outlet_address(outlet), receipts.HEAD_WIDTH, 3)
    assert len(lines) == 3 and lines[-1].endswith("...")
    assert all(pdf.get_string_width(line) <= receipts.HEAD_WIDTH for line in lines)
    assert receipts._when(order, tz=ZoneInfo("Asia/Kolkata")) == "30 Sep 2026, 6:35 PM IST"
    assert receipts._when(order, order.delivered_at,
                          tz=ZoneInfo("America/New_York")) == "30 Sep 2026, 3:20 PM EDT"
