"""Kitchen order visibility and notification retries through the real admin UI."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin import router
from app.admin.deps import require_admin
from app.db.session import get_session
from app.db.models import AdminUser, Order, OrderStage, PaymentStatus
from app.services import kitchen_alerts


@pytest.fixture
def admin(session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_admin] = lambda: AdminUser(
        email="ops@shero.test", name="Ops", password_hash="x", is_active=True)
    return TestClient(app, follow_redirects=False)


async def add_order(session, customer, outlet, number, dish):
    order = Order(order_number=number, customer_id=customer.id, outlet_id=outlet.id,
                  total=Decimal("20"), items=[{"quantity": 2, "name": dish}], item_count=2,
                  slot_label="Tomorrow, 6 PM", payment_status=PaymentStatus.PAID,
                  stage=OrderStage.PAID_SLOT_BOOKED,
                  kitchen_notify_at=datetime.now(timezone.utc))
    session.add(order)
    await session.flush()
    return order


async def test_partner_filter_shows_only_its_orders_and_items(admin, session, customer, outlet, far_outlet):
    outlet.kitchen_whatsapp = "14438011011"
    await add_order(session, customer, outlet, "SHO-EDISON-1", "Carrot Sambar")
    await add_order(session, customer, far_outlet, "SHO-JERSEY-1", "Appam")
    response = admin.get(f"/admin/orders?kitchen_id={outlet.id}")
    assert response.status_code == 200
    assert "SHO-EDISON-1" in response.text and "Carrot Sambar" in response.text
    assert "SHO-JERSEY-1" not in response.text and "Appam" not in response.text
    assert "14438011011" in response.text
    assert "Retry kitchen alert" in response.text
    assert "Alert pending / retrying" in response.text
    assert f"kitchen_id={outlet.id}" in response.text
    assert admin.get("/admin/orders?kitchen_id=invalid").status_code == 422
    assert admin.get("/admin/orders?kitchen_id=").status_code == 200
    assert admin.get("/admin/orders").status_code == 200


async def test_kitchens_link_to_their_queue_with_active_counts(admin, session, customer, outlet):
    await add_order(session, customer, outlet, "SHO-EDISON-1", "Sambar")
    response = admin.get("/admin/kitchens")
    assert response.status_code == 200
    assert "View orders (1 active)" in response.text
    assert f"kitchen_id={outlet.id}" in response.text
    edit = admin.get(f"/admin/kitchens/{outlet.id}")
    assert edit.status_code == 200
    assert "New paid orders are sent here automatically" in edit.text


async def test_admin_retry_sends_then_displays_accepted_status(admin, session, customer, outlet, monkeypatch):
    outlet.kitchen_whatsapp = "14438011011"
    order = await add_order(session, customer, outlet, "SHO-RETRY-1", "Sambar")
    sent = []
    class Sender:
        async def send_template(self, to, spec, *values):
            sent.append((to, values))
    async def nothing(*args, **kwargs):
        return True
    monkeypatch.setattr(kitchen_alerts, "current_sender", lambda: Sender())
    monkeypatch.setattr(kitchen_alerts.crm_sync, "push_order_stage", nothing)
    response = admin.post(f"/admin/orders/{order.order_number}/notify-kitchen")
    assert response.status_code == 303
    assert sent[0][0] == outlet.kitchen_whatsapp
    assert "2 x Sambar" in sent[0][1][-1]
    page = admin.get(f"/admin/orders?kitchen_id={outlet.id}")
    assert "WhatsApp alert accepted" in page.text
    assert "Retry kitchen alert" not in page.text
    admin.post(f"/admin/orders/{order.order_number}/notify-kitchen")
    assert len(sent) == 1


async def test_admin_retry_does_not_send_unpaid_order(admin, session, customer, outlet, monkeypatch):
    order = await add_order(session, customer, outlet, "SHO-UNPAID-1", "Sambar")
    order.payment_status = PaymentStatus.LINK_SENT
    order.stage = OrderStage.PENDING_PAYMENT
    await session.flush()
    response = admin.post(f"/admin/orders/{order.order_number}/notify-kitchen")
    assert response.status_code == 303
    assert order.kitchen_notified_at is None
