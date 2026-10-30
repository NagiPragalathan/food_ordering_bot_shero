"""The Payouts page: the split (one for every kitchen, or each kitchen's own)
and every paid order with what its kitchen and Shero get."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin import router
from app.admin.deps import require_admin
from app.db.models import AdminUser, Order, OrderStage, PaymentStatus
from app.db.session import get_session
from app.services import payouts

D = Decimal


@pytest.fixture
def admin(session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_admin] = lambda: AdminUser(
        email="ops@shero.test", name="Ops", password_hash="x", is_active=True)
    return TestClient(app, follow_redirects=False)


async def order(session, customer, outlet, number, food="100.00"):
    row = Order(order_number=number, customer_id=customer.id, outlet_id=outlet.id,
                items=[{"retailer_id": "sambar", "name": "Drumstick Sambar", "quantity": 8,
                        "unit_price": "12.50", "line_total": food}],
                dish_total=D(food), delivery_fee=D("8.00"), total=D(food) + D("8.00"),
                payment_status=PaymentStatus.PAID, stage=OrderStage.DELIVERED,
                paid_at=datetime.now(timezone.utc))
    session.add(row)
    await session.flush()
    return row


async def test_the_page_lists_the_orders_with_the_split(admin, session, customer, outlet,
                                                       far_outlet):
    await order(session, customer, outlet, "SHO-PAY-1")
    await order(session, customer, far_outlet, "SHO-PAY-2")
    page = admin.get("/admin/payouts")
    assert page.status_code == 200
    for text in ("Same split for every kitchen", "Kitchen partner gets", "Shero gets",
                 "SHO-PAY-1", "SHO-PAY-2", "$108.00", "$65.00", "$35.00", "By kitchen",
                 "8 × Drumstick Sambar", "Save split"):
        assert text in page.text, text
    for gone in ("Record payment", "Payout history", "PPP", "Use today&#39;s split"):
        assert gone not in page.text, gone

    one = admin.get(f"/admin/payouts?kitchen={outlet.id}&period=all").text
    assert "SHO-PAY-1" in one and "SHO-PAY-2" not in one
    orders = admin.get("/admin/orders").text
    assert "Kitchen $65.00 · Shero $35.00" in orders                       # on each paid order


async def test_saving_the_shared_split(admin, session, customer, outlet):
    await order(session, customer, outlet, "SHO-PAY-3")
    saved = admin.post("/admin/payouts/split", data={
        "same_for_all": "on", "kitchen_percent": "70", "period": "all"})
    assert saved.status_code == 303 and saved.headers["location"].endswith("?period=all")
    split = await payouts.settings(session)
    assert (split.same_for_all, split.kitchen_percent) == (True, D("70.00"))
    assert "$70.00" in admin.get("/admin/payouts").text


async def test_saving_each_kitchens_own_split(admin, session, customer, outlet, far_outlet):
    await order(session, customer, far_outlet, "SHO-PAY-4")
    admin.post("/admin/payouts/split", data={
        "kitchen_percent": "65", f"kitchen_percent_{far_outlet.id}": "80",
        f"kitchen_percent_{outlet.id}": ""})
    split = await payouts.settings(session)
    assert split.same_for_all is False and split.kitchens == {far_outlet.id: D("80.00")}
    page = admin.get("/admin/payouts").text
    assert "$80.00" in page and "$20.00" in page


async def test_a_bad_percentage_is_not_saved(admin, session):
    response = admin.post("/admin/payouts/split", data={"same_for_all": "on",
                                                       "kitchen_percent": "150"})
    assert response.status_code == 303
    assert (await payouts.settings(session)).kitchen_percent == D("65.00")


async def test_the_csv(admin, session, customer, outlet):
    await order(session, customer, outlet, "SHO-PAY-5")
    response = admin.get("/admin/payouts/orders.csv?period=all")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv")
    assert "SHO-PAY-5" in response.text and "Kitchen gets" in response.text
