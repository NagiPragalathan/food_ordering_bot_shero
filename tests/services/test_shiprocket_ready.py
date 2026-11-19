"""Mark ready books a Shiprocket rider for a prepared DST India order, and
the customer hears "being prepared" right after paying
(services/shiprocket_booking.py, integrations/shiprocket/client.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from app.core import tenants
from app.core.config import settings_for
from app.db.models import DeliverySlot, Order, OrderStage, PaymentStatus
from app.integrations.gallabox import templates as tpl
from app.integrations.shiprocket import client as sr
from app.services import payments, shiprocket_booking

D = Decimal


@pytest.fixture
def connected(monkeypatch):
    """DST India with a Shiprocket API user (never a real one: no network)."""
    dst = settings_for(tenants.DST)
    monkeypatch.setattr(dst, "shiprocket_email", "bot@shero.test")
    monkeypatch.setattr(dst, "shiprocket_password", "not-a-real-password")
    monkeypatch.setattr(sr, "_tokens", {})
    with tenants.use(tenants.DST):
        yield dst


@pytest.fixture
def kitchen(outlet):
    outlet.name, outlet.city, outlet.state, outlet.postal_code = "Anna Nagar", "Chennai", "TN", "600040"
    outlet.timezone = "Asia/Kolkata"
    outlet.shiprocket_pickup = "Anna Nagar Kitchen"
    return outlet


async def _order(session, customer, outlet, number="DST-261119-AAAAA", *,
                 stage=OrderStage.SENT_TO_KITCHEN, paid=True) -> Order:
    start = datetime.now(timezone.utc) + timedelta(minutes=30)
    slot = DeliverySlot(outlet_id=outlet.id, starts_at=start, ends_at=start + timedelta(hours=1),
                        capacity=5, reserved_count=1)
    session.add(slot)
    await session.flush()
    order = Order(
        order_number=number, customer_id=customer.id, outlet_id=outlet.id, slot_id=slot.id,
        items=[{"retailer_id": "IDLI4", "name": "Idli (4)", "quantity": 2, "unit_price": "60.00"},
               {"retailer_id": "DOSA", "name": "Masala Dosa", "quantity": 1, "unit_price": "110.00"}],
        dish_total=D("230.00"), delivery_fee=D("40.00"), tax=D("11.50"), discount=D("20.00"),
        total=D("261.50"), currency="INR", contact_number="+91 98400 12345",
        delivery_address="12 2nd Avenue, Anna Nagar", apartment_unit="Flat 3B",
        delivery_instructions="Ring twice", postal_code="600040",
        delivery_latitude=13.0850, delivery_longitude=80.2101,
        payment_status=PaymentStatus.PAID if paid else PaymentStatus.LINK_SENT, stage=stage)
    session.add(order)
    await session.flush()
    return order


class FakeShiprocket:
    """Stands in for the API calls; records what was asked."""

    def __init__(self, monkeypatch, *, rates=None, assign=None):
        self.created, self.assigned, self.cancelled, self.pickups = [], [], [], []
        self.refuse_cancel: str | None = None
        self.refuse_pickup: str | None = None
        self.existing_pickups = ["Anna Nagar Kitchen"]
        self.rates = [{"id": 7, "name": "Borzo", "rate": 58.0}] if rates is None else rates
        self.assign = assign or {"awb_assign_status": 1, "response": {"data": {
            "awb_code": "SRQ123", "courier_name": "Borzo"}}}

        async def create_order(payload):
            self.created.append(payload)
            n = len(self.created)
            return {"order_id": 9000 + n, "shipment_id": 7000 + n, "status": "NEW"}

        async def cancel_order(order_id):
            if self.refuse_cancel:
                raise sr.ShiprocketError(self.refuse_cancel)
            self.cancelled.append(str(order_id))
            return {"status_code": 200, "message": "Order cancelled successfully."}

        async def hyperlocal_rates(**kw):
            return self.rates

        async def assign_rider(shipment_id, courier_id=None):
            self.assigned.append((shipment_id, courier_id))
            return self.assign

        async def add_pickup(payload):
            if self.refuse_pickup:
                raise sr.ShiprocketError(self.refuse_pickup)
            self.pickups.append(payload)
            self.existing_pickups.append(payload["pickup_location"])
            return {"success": True, "pickup_id": 555}

        async def pickup_locations():
            return list(self.existing_pickups)

        for name, fn in (("create_order", create_order), ("hyperlocal_rates", hyperlocal_rates),
                         ("assign_rider", assign_rider), ("cancel_order", cancel_order),
                         ("add_pickup", add_pickup), ("pickup_locations", pickup_locations)):
            monkeypatch.setattr(sr, name, fn)


# --- the order Shiprocket is sent ------------------------------------------------------------
async def test_the_order_goes_to_shiprocket_as_a_prepaid_hyperlocal_drop(session, customer, kitchen,
                                                                       connected):
    order = await _order(session, customer, kitchen)
    body = shiprocket_booking.order_payload(order, kitchen, customer)
    assert body["shipping_method"] == "HL" and body["payment_method"] == "Prepaid"
    assert body["pickup_location"] == "Anna Nagar Kitchen"
    assert (body["latitude"], body["longitude"]) == (13.0850, 80.2101)
    assert body["order_id"] == "DST-261119-AAAAA"
    assert body["billing_customer_name"] == "Asha" and body["billing_last_name"] == "Menon"
    assert body["billing_phone"] == "9840012345"
    assert body["billing_city"] == "Chennai" and body["billing_state"] == "Tamil Nadu"
    assert body["billing_address_2"] == "Flat 3B, Ring twice"
    assert [(i["sku"], i["units"], i["selling_price"]) for i in body["order_items"]] == \
        [("IDLI4", 2, 60.0), ("DOSA", 1, 110.0)]
    # What the customer paid, split as Shiprocket adds it up.
    assert body["shipping_charges"] == 40.0 and body["sub_total"] == 221.5
    assert body["weight"] == 1.0


# --- Mark ready ------------------------------------------------------------------------------
async def test_mark_ready_books_the_cheapest_rider_once(session, customer, kitchen, connected,
                                                        monkeypatch):
    fake = FakeShiprocket(monkeypatch)
    order = await _order(session, customer, kitchen)

    result = await shiprocket_booking.mark_ready(session, order)
    assert result.ok and "AWB SRQ123" in result.message
    assert fake.assigned == [("7001", 7)]
    info = order.courier
    assert info["assigned"] and info["awb"] == "SRQ123" and info["courier_name"] == "Borzo"
    assert info["shipment_id"] == "7001" and info["fee"] == 58.0 and info["error"] is None
    assert info["tracking_url"] == "https://shiprocket.co/tracking/SRQ123"
    assert order.ready_at is not None
    # The stage stays with the staff: Out for delivery is still their button.
    assert order.stage == OrderStage.SENT_TO_KITCHEN

    again = await shiprocket_booking.mark_ready(session, order)
    assert again.ok and "already" in again.message
    assert len(fake.created) == 1 and len(fake.assigned) == 1


async def test_a_refused_rider_can_be_tried_again_on_the_same_shipment(session, customer, kitchen,
                                                                      connected, monkeypatch):
    fake = FakeShiprocket(monkeypatch, assign={
        "awb_assign_status": 0, "response": {"data": {"awb_assign_error": "No rider nearby"}}})
    order = await _order(session, customer, kitchen)
    result = await shiprocket_booking.mark_ready(session, order)
    assert not result.ok and "No rider nearby" in result.message
    assert order.courier["error"] == "No rider nearby" and not order.courier.get("assigned")
    assert shiprocket_booking.can_mark_ready(order)

    fake.assign = {"success": True, "message": "We are processing your request"}
    retry = await shiprocket_booking.mark_ready(session, order)
    assert retry.ok and "assigning one now" in retry.message
    assert len(fake.created) == 1            # the same Shiprocket order, not a second one
    assert order.courier["assigned"] and order.courier["status"] == "Assigning a rider"


async def test_shiprocket_quick_has_no_courier_id_and_its_price_is_kept(session, customer, kitchen,
                                                                       connected, monkeypatch):
    # What the live account answers for a hyperlocal drop (checked 19 Nov 2026).
    fake = FakeShiprocket(monkeypatch, rates=sr.couriers(
        {"status": True, "data": [{"courier_name": "Shiprocket Quick", "rates": 65, "etd_hours": 1}]}))
    order = await _order(session, customer, kitchen)
    result = await shiprocket_booking.mark_ready(session, order)
    assert result.ok
    assert fake.assigned == [("7001", None)]                 # Shiprocket assigns its Quick rider
    assert order.courier["fee"] == 65.0 and order.courier["courier_name"] == "Borzo"


async def test_cancel_booking_releases_the_rider_and_mark_ready_books_afresh(session, customer, kitchen,
                                                                           connected, monkeypatch):
    fake = FakeShiprocket(monkeypatch)
    order = await _order(session, customer, kitchen)
    await shiprocket_booking.mark_ready(session, order)
    assert shiprocket_booking.can_cancel(order)

    result = await shiprocket_booking.cancel_booking(session, order)
    assert result.ok and "cancelled" in result.message and "rider released" in result.message
    assert fake.cancelled == ["9001"]
    info = order.courier
    assert not info["assigned"] and info["shipment_id"] is None and info["awb"] is None
    assert info["status"] == "Cancelled on Shiprocket"
    assert info["cancelled"][0]["order_id"] == "9001" and info["cancelled"][0]["awb"] == "SRQ123"
    assert shiprocket_booking.can_mark_ready(order) and not shiprocket_booking.can_cancel(order)

    # Booked again: a new Shiprocket order, under a new id (the old one stays cancelled there).
    again = await shiprocket_booking.mark_ready(session, order)
    assert again.ok and len(fake.created) == 2
    assert [p["order_id"] for p in fake.created] == ["DST-261119-AAAAA", "DST-261119-AAAAA-2"]
    assert order.courier["order_id"] == "9002" and order.courier["assigned"]


async def test_a_refused_cancel_keeps_the_booking(session, customer, kitchen, connected, monkeypatch):
    fake = FakeShiprocket(monkeypatch)
    order = await _order(session, customer, kitchen)
    await shiprocket_booking.mark_ready(session, order)
    fake.refuse_cancel = "Shipment already picked up"
    result = await shiprocket_booking.cancel_booking(session, order)
    assert not result.ok and "already picked up" in result.message
    assert order.courier["assigned"] and order.courier["awb"] == "SRQ123"


async def test_nothing_to_cancel_is_said_plainly(session, customer, kitchen, connected, monkeypatch):
    fake = FakeShiprocket(monkeypatch)
    order = await _order(session, customer, kitchen)
    result = await shiprocket_booking.cancel_booking(session, order)
    assert not result.ok and "no Shiprocket booking" in result.message and fake.cancelled == []


async def test_cancelling_the_order_releases_its_shiprocket_rider(session, customer, kitchen, connected,
                                                                  monkeypatch):
    from app.api.routes import ops

    fake = FakeShiprocket(monkeypatch)
    order = await _order(session, customer, kitchen)
    await shiprocket_booking.mark_ready(session, order)

    async def nothing(*a, **kw):
        return False

    monkeypatch.setattr(ops.gallabox, "send_template", nothing)
    monkeypatch.setattr(ops.payments, "refund_order", nothing)
    monkeypatch.setattr(ops.crm_sync, "push_order_stage", nothing)
    await ops.cancel_order(order.order_number, None, session)
    assert order.stage == OrderStage.CANCELLED
    assert fake.cancelled == ["9001"] and not order.courier["assigned"]


async def test_no_rider_offered_is_said_plainly(session, customer, kitchen, connected, monkeypatch):
    FakeShiprocket(monkeypatch, rates=[])
    order = await _order(session, customer, kitchen)
    result = await shiprocket_booking.mark_ready(session, order)
    assert not result.ok and "no rider is available" in result.message


@pytest.mark.parametrize("problem", ["unpaid", "delivered", "no_pin"])
async def test_mark_ready_refuses_what_it_cannot_book(session, customer, kitchen, connected,
                                                      monkeypatch, problem):
    fake = FakeShiprocket(monkeypatch)
    order = await _order(session, customer, kitchen, paid=problem != "unpaid",
                         stage=OrderStage.DELIVERED if problem == "delivered" else OrderStage.SENT_TO_KITCHEN)
    if problem == "no_pin":
        order.delivery_latitude = None
    result = await shiprocket_booking.mark_ready(session, order)
    assert not result.ok and fake.created == [] and not order.courier


async def test_without_shiprocket_or_for_shero_us_there_is_no_mark_ready(session, customer, kitchen,
                                                                         monkeypatch):
    # Whatever a local .env holds: DST India without an API user.
    monkeypatch.setattr(settings_for(tenants.DST), "shiprocket_email", "")
    order = await _order(session, customer, kitchen)
    assert not shiprocket_booking.enabled()                  # Shero US: Uber
    with tenants.use(tenants.DST):
        assert not shiprocket_booking.enabled()              # DST India, no API user yet
        result = await shiprocket_booking.mark_ready(session, order)
    assert not result.ok and "not connected" in result.message


# --- the API client --------------------------------------------------------------------------
async def test_the_client_signs_in_once_and_again_after_a_401(connected, monkeypatch):
    calls = []
    expired = {"once": True}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, json={"token": f"tok{len(calls)}"})
        if expired["once"] and request.headers.get("authorization") == "Bearer tok1":
            expired["once"] = False
            return httpx.Response(401, json={"message": "Token has expired"})
        return httpx.Response(200, json={"data": {"shipping_address": [
            {"pickup_location": "Anna Nagar Kitchen"}, {"pickup_location": "Adyar"}]}})

    real = httpx.AsyncClient
    monkeypatch.setattr(sr.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    assert await sr.pickup_locations() == ["Anna Nagar Kitchen", "Adyar"]
    assert await sr.pickup_locations() == ["Anna Nagar Kitchen", "Adyar"]
    logins = [c for c in calls if c[1].endswith("/auth/login")]
    assert len(logins) == 2                                  # first, and after the 401 only


async def test_a_wrong_api_user_is_reported(connected, monkeypatch):
    def handler(request):
        return httpx.Response(400, json={"message": "Invalid email and password combination"})

    real = httpx.AsyncClient
    monkeypatch.setattr(sr.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(sr.ShiprocketError, match="sign-in failed.*Invalid email"):
        await sr.pickup_locations()


def test_answers_are_read_in_either_shape():
    hyperlocal = {"status": True, "data": [{"courier_name": "Shiprocket Quick", "rates": "345.3"},
                                           {"courier_company_id": 12, "courier_name": "Porter", "rates": 80}]}
    assert sr.couriers(hyperlocal) == [{"id": 12, "name": "Porter", "rate": 80.0},
                                       {"id": None, "name": "Shiprocket Quick", "rate": 345.3}]
    standard = {"data": {"available_courier_companies": [{"courier_company_id": 3, "courier_name": "X",
                                                          "rate": 50}]}}
    assert sr.couriers(standard) == [{"id": 3, "name": "X", "rate": 50.0}]
    assert sr.assignment({"success": True, "message": "We are processing your request"}) == \
        {"awb": None, "courier": None, "error": None}
    track = sr.tracking({"tracking_data": {"track_url": "https://shiprocket.co/tracking/1", "etd": "12:40",
                                           "shipment_track": [{"current_status": "PICKED UP",
                                                               "awb_code": "1", "courier_name": "Borzo",
                                                               "courier_agent_details": {"name": "Ravi",
                                                                                         "mobile": "98400"}}]}})
    assert track["status"] == "PICKED UP" and track["rider"] == "Ravi 98400" and track["eta"] == "12:40"


# --- "being prepared" ------------------------------------------------------------------------
async def test_dst_india_says_being_prepared_after_payment_and_shero_us_does_not(customer, monkeypatch):
    sent = []

    class Sender:
        async def send_template(self, to, spec, *values, **kw):
            sent.append((spec.name, values))

    monkeypatch.setattr(payments, "current_sender", lambda: Sender())
    order = Order(order_number="DST-261119-PPPPP")
    assert await payments.send_preparing_message(customer, order, "Anna Nagar") is False
    with tenants.use(tenants.DST):
        assert await payments.send_preparing_message(customer, order, "Anna Nagar") is True
    assert sent == [(tpl.ORDER_PREPARING.name, ("DST-261119-PPPPP", "Anna Nagar"))]
    assert tpl.ORDER_PREPARING.sample_body.startswith("Your order #{{1}} is being prepared at {{2}}")


# --- the admin pages -------------------------------------------------------------------------
def _admin(session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.admin import router
    from app.admin.deps import require_admin
    from app.db.models import AdminUser
    from app.db.session import get_session

    app = FastAPI()
    app.include_router(router)
    app.add_middleware(tenants.TenantPathMiddleware)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_admin] = lambda: AdminUser(
        email="ops@shero.test", name="Ops", password_hash="x", is_active=True)
    return TestClient(app)


async def test_mark_ready_on_the_orders_page_books_and_returns(session, customer, kitchen, connected,
                                                               monkeypatch):
    FakeShiprocket(monkeypatch)
    order = await _order(session, customer, kitchen)
    client = _admin(session)
    page = client.get("/dst/admin/orders")
    assert page.status_code == 200 and "Mark ready" in page.text

    done = client.post(f"/dst/admin/orders/{order.order_number}/ready",
                       data={"back": "http://testserver/dst/admin/deliveries"}, follow_redirects=False)
    assert done.status_code in (302, 303)
    assert done.headers["location"] == "http://testserver/dst/admin/deliveries"
    assert order.courier["assigned"] and order.courier["awb"] == "SRQ123"

    queue = client.get("/dst/admin/deliveries")
    assert queue.status_code == 200
    assert "Shiprocket rider" in queue.text and "SRQ123" in queue.text and "Check rider" in queue.text
    assert "Cancel booking" in queue.text and "₹58" in queue.text

    cancelled = client.post(f"/dst/admin/orders/{order.order_number}/rider/cancel",
                            data={"back": "http://testserver/dst/admin/deliveries"}, follow_redirects=False)
    assert cancelled.status_code in (302, 303)
    assert cancelled.headers["location"] == "http://testserver/dst/admin/deliveries"
    assert not order.courier["assigned"]
    queue = client.get("/dst/admin/deliveries")
    assert "Last Shiprocket booking cancelled" in queue.text and "Mark ready" in queue.text


async def test_a_kitchen_without_a_pickup_still_shows_mark_ready_and_says_what_it_does(
        session, customer, kitchen, connected, monkeypatch):
    FakeShiprocket(monkeypatch)
    kitchen.shiprocket_pickup = None
    await _order(session, customer, kitchen)
    client = _admin(session)
    for page in (client.get("/dst/admin/orders"), client.get("/dst/admin/deliveries")):
        assert page.status_code == 200
        assert "Mark ready" in page.text
        assert "its address goes to Shiprocket as its pickup" in page.text


# --- a kitchen's pickup location, made from its own record ------------------------------------
@pytest.fixture
def new_kitchen(kitchen):
    kitchen.name = "Shero Home Food-Chettinad SKID-IND-1263 A"
    kitchen.address_line1 = "Door No.27, Flat 2B, 2nd floor, Barracks Road, Periamet"
    kitchen.kitchen_whatsapp, kitchen.phone = "919884924086", "+918690666666"
    kitchen.latitude, kitchen.longitude = 13.0870, 80.2620
    kitchen.shiprocket_pickup = None
    return kitchen


async def test_mark_ready_adds_a_kitchen_without_a_pickup_to_shiprocket_and_books(
        session, customer, new_kitchen, connected, monkeypatch):
    fake = FakeShiprocket(monkeypatch)
    order = await _order(session, customer, new_kitchen)
    result = await shiprocket_booking.mark_ready(session, order)
    assert result.ok and order.courier["assigned"]

    sent = fake.pickups[0]
    assert sent["pickup_location"] == "DST SKID-1263 A"
    assert sent["phone"] == "9884924086"                      # the cook, not the head-office line
    assert sent["address"].startswith("Door No.27") and sent["pin_code"] == "600040"
    assert sent["city"] == "Chennai" and sent["state"] == "Tamil Nadu" and sent["country"] == "India"
    assert (sent["lat"], sent["long"]) == (13.0870, 80.2620)
    assert new_kitchen.shiprocket_pickup == "DST SKID-1263 A"  # kept for next time
    assert fake.created[0]["pickup_location"] == "DST SKID-1263 A"

    second = await _order(session, customer, new_kitchen, number="DST-261119-BBBBB")
    await shiprocket_booking.mark_ready(session, second)
    assert len(fake.pickups) == 1                             # made once


async def test_a_pickup_made_by_an_earlier_press_is_used_not_made_again(
        session, customer, new_kitchen, connected, monkeypatch):
    fake = FakeShiprocket(monkeypatch)
    fake.existing_pickups.append("DST SKID-1263 A")
    fake.refuse_pickup = "Address nick name already in use"
    order = await _order(session, customer, new_kitchen)
    result = await shiprocket_booking.mark_ready(session, order)
    assert result.ok and new_kitchen.shiprocket_pickup == "DST SKID-1263 A"


async def test_an_address_shiprocket_refuses_is_said_and_nothing_is_booked(
        session, customer, new_kitchen, connected, monkeypatch):
    fake = FakeShiprocket(monkeypatch)
    fake.refuse_pickup = "Address line 1 should have House no / Flat no / Road no."
    order = await _order(session, customer, new_kitchen)
    result = await shiprocket_booking.mark_ready(session, order)
    assert not result.ok and "House no" in result.message and "Kitchens page" in result.message
    assert fake.created == [] and new_kitchen.shiprocket_pickup is None
    assert "House no" in order.courier["error"]               # shown on the order, with Try again


def test_long_kitchen_addresses_are_split_into_shiprocket_s_two_lines():
    line1, line2 = shiprocket_booking._split_address(
        "No:2 Ground Floor third Street Extension, Ramani Nagar, Krishna Nagar West, Tambaram, Chennai")
    assert len(line1) <= 80 and line1.endswith("Krishna Nagar West")
    assert line2 == "Tambaram, Chennai"
