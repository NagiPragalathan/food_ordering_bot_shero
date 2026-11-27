"""The customer's WhatsApp at three stages - Food Ready, Food Dispatched and
Order Delivered, with the invoice (services/order_updates.py) - through the
real admin pages.

The client: "Trigger messages at three stages: Food Ready, Food Dispatched
and Order Delivered. Attach the invoice to the Order Delivered message." and
"Generate and send invoices only after order delivery, not upon order
placement."
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin import router
from app.admin.deps import require_admin
from app.admin.routes.orders import TRANSITIONS
from app.core.config import settings
from app.db.models import AdminUser, InboundMessage, Order, OrderStage, PaymentStatus
from app.db.session import get_session
from app.integrations.gallabox import templates as tpl
from app.services import order_updates, receipts

pytestmark = pytest.mark.asyncio


class Sender:
    def __init__(self):
        self.sent: list[tuple] = []
        self.on_document = None

    async def send_template(self, to, spec, *values, button_value=None):
        self.sent.append(("template", to, spec.name, values, button_value))

    async def send_document(self, to, *, url, filename, caption=None):
        if self.on_document:
            self.on_document()
        self.sent.append(("document", to, url, filename, caption))


@pytest.fixture
def sender(monkeypatch):
    sender = Sender()
    monkeypatch.setattr(order_updates, "current_sender", lambda: sender)
    monkeypatch.setattr(settings, "admin_session_secret", "s")
    monkeypatch.setattr(settings, "public_base_url", "https://shero.test")
    return sender


@pytest.fixture
def admin(session, monkeypatch):
    async def nothing(*_a, **_kw):
        return None
    from app.admin.routes import orders as route
    monkeypatch.setattr(route.crm_sync, "push_order_stage", nothing)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_admin] = lambda: AdminUser(
        email="ops@shero.test", name="Ops", password_hash="x", is_active=True)
    return TestClient(app, follow_redirects=False)


async def _order(session, customer, outlet, number="SHO-261127-STEP1") -> Order:
    order = Order(order_number=number, customer_id=customer.id, outlet_id=outlet.id,
                  total=Decimal("20"), items=[{"quantity": 2, "name": "Beans Sambar"}],
                  item_count=2, slot_label="Today, 6 PM", payment_status=PaymentStatus.PAID,
                  stage=OrderStage.SENT_TO_KITCHEN,
                  paid_at=datetime.now(timezone.utc))
    session.add(order)
    await session.flush()
    return order


async def _wrote(session, customer, hours_ago: float) -> None:
    session.add(InboundMessage(provider_message_id=f"wamid.{hours_ago}",
                               whatsapp_number=customer.whatsapp_number, payload={}))
    await session.flush()
    (await session.execute(InboundMessage.__table__.update().values(
        created_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago))))


def _post(admin, order, step):
    return admin.post(f"/admin/orders/{order.order_number}/{step}", data={"back": ""})


async def test_three_messages_and_the_invoice_only_on_delivery(
        session, customer, outlet, admin, sender):
    order = await _order(session, customer, outlet)
    await _wrote(session, customer, hours_ago=2)      # inside WhatsApp's window
    assert not receipts.invoiced(order)

    # 1. Food Ready: the page's step is Food ready, not Out for delivery.
    advance = f"/admin/orders/{order.order_number}/advance"
    page = admin.get("/admin/orders").text
    assert "data-food-ready" in page and advance not in page
    assert _post(admin, order, "ready").status_code == 303
    page = admin.get("/admin/orders").text
    assert "data-food-ready" not in page and advance in page and "Food ready" in page
    assert order.ready_at is not None and order.stage == OrderStage.SENT_TO_KITCHEN
    assert sender.sent[-1][:3] == ("template", customer.whatsapp_number, "order_food_ready")
    assert sender.sent[-1][3] == (order.order_number, outlet.name)
    _post(admin, order, "ready")                       # once only
    assert len(sender.sent) == 1

    # 2. Food Dispatched.
    _post(admin, order, "advance")
    assert order.stage == OrderStage.OUT_FOR_DELIVERY
    assert sender.sent[-1][2:4] == ("order_out_for_delivery", (order.order_number,))
    assert not receipts.invoiced(order)               # still no invoice

    # 3. Order Delivered: inside the window, the invoice PDF itself - sent
    # once the delivery is committed, as WhatsApp fetches it at once.
    pending = []
    sender.on_document = lambda: pending.append(session.in_transaction())
    done = _post(admin, order, "advance")
    assert pending == [False]
    assert order.stage == OrderStage.DELIVERED and receipts.invoiced(order)
    kind, to, url, filename, caption = sender.sent[-1]
    assert kind == "document" and to == customer.whatsapp_number
    assert receipts.read_token(url.rsplit("/", 1)[1]) == order.order_number
    assert filename == f"Shero-invoice-{order.order_number}.pdf"
    assert caption.startswith(f"Your order #{order.order_number} has been delivered")
    assert done.status_code == 303
    assert len(sender.sent) == 3


async def test_outside_the_window_the_template_carries_the_invoice_button(
        session, customer, outlet, sender):
    order = await _order(session, customer, outlet)
    await _wrote(session, customer, hours_ago=30)
    assert await order_updates.notify_delivered(session, order)
    kind, _, name, values, button = sender.sent[-1]
    assert (kind, name, values) == ("template", "order_delivered_v2", (order.order_number,))
    assert receipts.read_token(button) == order.order_number
    assert tpl.ORDER_DELIVERED.button_label == "Download Invoice"
    assert tpl.ORDER_DELIVERED.url_base == "receipt"


async def test_until_meta_approves_it_the_first_delivered_template_goes(
        session, customer, outlet, sender, monkeypatch):
    """Outside the window an unapproved template would be dropped: the
    customer still hears it was delivered, on the approved first one."""
    order = await _order(session, customer, outlet)

    async def approved(name):
        return name == tpl.ORDER_DELIVERED_V1.name
    monkeypatch.setattr(order_updates.template_status, "is_approved", approved)
    assert await order_updates.notify_delivered(session, order)
    assert sender.sent[-1][2:5] == ("order_delivered", (order.order_number,), None)


async def test_switched_off_the_stages_move_and_nobody_is_messaged(
        session, customer, outlet, admin, sender, monkeypatch):
    monkeypatch.setattr(settings, "send_delivery_updates", False)
    order = await _order(session, customer, outlet)
    _post(admin, order, "ready")
    _post(admin, order, "advance")
    _post(admin, order, "advance")
    assert order.ready_at is not None and order.stage == OrderStage.DELIVERED
    assert sender.sent == []


async def test_an_order_not_with_the_kitchen_cannot_be_marked_ready(
        session, customer, outlet, sender):
    order = await _order(session, customer, outlet)
    order.payment_status = PaymentStatus.LINK_SENT
    assert not (await order_updates.mark_ready(session, order)).ok
    order.payment_status, order.stage = PaymentStatus.PAID, OrderStage.DELIVERED
    assert not (await order_updates.mark_ready(session, order)).ok
    assert order.ready_at is None and sender.sent == []


async def test_the_ops_api_marks_ready_and_messages_each_stage(
        session, customer, outlet, sender, monkeypatch):
    from app.api.routes import ops
    monkeypatch.setattr(settings, "ops_api_key", "k")

    async def nothing(*_a, **_kw):
        return None
    monkeypatch.setattr(ops.crm_sync, "push_order_stage", nothing)
    app = FastAPI()
    app.include_router(ops.router)
    app.dependency_overrides[get_session] = lambda: session
    client = TestClient(app)
    order = await _order(session, customer, outlet)
    head = {"X-Ops-Key": "k"}
    assert client.post(f"/ops/orders/{order.order_number}/ready", headers=head).status_code == 200
    assert client.post(f"/ops/orders/{order.order_number}/out-for-delivery",
                       headers=head).status_code == 200
    assert client.post(f"/ops/orders/{order.order_number}/delivered",
                       headers=head).status_code == 200
    assert [s[2] for s in sender.sent] == ["order_food_ready", "order_out_for_delivery",
                                          "order_delivered_v2"]


def test_the_stages_and_the_messages_are_on():
    assert TRANSITIONS[OrderStage.PAID_SLOT_BOOKED][1] is OrderStage.SENT_TO_KITCHEN
    assert TRANSITIONS[OrderStage.SENT_TO_KITCHEN][1] is OrderStage.OUT_FOR_DELIVERY
    assert TRANSITIONS[OrderStage.OUT_FOR_DELIVERY][1] is OrderStage.DELIVERED
    assert settings.send_delivery_updates is True
    assert settings.send_payment_reminder is False
    for spec in (tpl.ORDER_FOOD_READY, tpl.ORDER_OUT_FOR_DELIVERY, tpl.ORDER_DELIVERED,
                 tpl.ORDER_DELIVERED_V1):
        assert spec.name in tpl.BY_NAME
