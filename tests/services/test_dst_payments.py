"""DST India's money path on Razorpay, end to end through the same services
Shero US uses on Stripe: the link, the webhook, the signed return, the expiry
check and refunds. Nothing reaches Razorpay or WhatsApp."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.api.routes import pay, webhooks_razorpay
from app.core import tenants
from app.core.config import settings_for
from app.db.models import DeliverySlot, Order, OrderStage, PaymentStatus
from app.db.session import get_session
from app.integrations import payment_gateway
from app.integrations.razorpay_gw import checkout as rzp
from app.integrations.stripe_gw import checkout as stripe_checkout
from app.services import payments

ITEMS = [{"retailer_id": "SAMBAR-1", "name": "Drumstick Sambar", "quantity": 2,
          "unit_price": "249.00", "line_total": "498.00"}]
NUMBER = "DST-261116-ABCDE"


@pytest.fixture
def dst(monkeypatch):
    own = settings_for(tenants.DST)
    monkeypatch.setattr(own, "razorpay_key_id", "rzp_test_abc123")
    monkeypatch.setattr(own, "razorpay_key_secret", "key_secret_xyz")
    monkeypatch.setattr(own, "razorpay_webhook_secret", "hook_secret")
    monkeypatch.setattr(own, "public_base_url", "https://bot.example/dst")
    with tenants.use(tenants.DST):
        yield own


@pytest.fixture
def quiet(monkeypatch):
    """No WhatsApp, CRM, kitchen or team alerts; records the templates sent."""
    sent = []

    class Sender:
        async def send_template(self, to, spec, *values, **kw):
            sent.append((spec.name, values))

    async def nothing(*_a, **_k):
        return True
    monkeypatch.setattr(payments, "current_sender", lambda: Sender())
    monkeypatch.setattr(payments.crm_sync, "advance_stage", nothing)
    monkeypatch.setattr(payments.crm_sync, "convert_and_record_order", nothing)
    monkeypatch.setattr(payments.crm_sync, "push_order_stage", nothing)
    monkeypatch.setattr(payments.order_alerts, "notify_new_order", nothing)
    monkeypatch.setattr(payments.kitchen_alerts, "send_alert", nothing)
    return sent


@pytest.fixture
def razorpay(monkeypatch):
    """Razorpay answers from a script; every call is recorded."""
    recorded, answers = [], {}

    async def api(method, path, *, json_body=None, params=None):
        recorded.append((method, path, json_body))
        return answers.get((method, path))

    monkeypatch.setattr(rzp, "_api", api)
    return recorded, answers


async def _order(session, customer, outlet, **overrides) -> Order:
    start = datetime.now(timezone.utc) + timedelta(days=2)
    slot = DeliverySlot(outlet_id=outlet.id, starts_at=start, ends_at=start + timedelta(hours=1),
                        capacity=2, reserved_count=0)
    session.add(slot)
    await session.flush()
    fields = dict(order_number=NUMBER, customer_id=customer.id, outlet_id=outlet.id,
                  slot_id=slot.id, items=ITEMS, item_count=2, dish_total=Decimal("498.00"),
                  delivery_fee=Decimal("40.00"), tax=Decimal("24.90"),
                  total=Decimal("562.90"), currency="INR", stripe_session_id="plink_1",
                  payment_status=PaymentStatus.LINK_SENT, stage=OrderStage.PENDING_PAYMENT,
                  payment_link_expires_at=datetime.now(timezone.utc) + timedelta(minutes=20),
                  slot_starts_at=start, slot_ends_at=start + timedelta(hours=1))
    fields.update(overrides)
    order = Order(**fields)
    session.add(order)
    await session.flush()
    return order


def _app(session) -> FastAPI:
    app = FastAPI()
    app.include_router(webhooks_razorpay.router)
    app.include_router(pay.router)
    app.add_middleware(tenants.TenantPathMiddleware)
    app.dependency_overrides[get_session] = lambda: session
    return app


async def _post_webhook(session, event: dict, *, path="/dst/webhooks/razorpay",
                        secret="hook_secret"):
    body = json.dumps(event).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    transport = httpx.ASGITransport(app=_app(session))
    async with httpx.AsyncClient(transport=transport, base_url="http://bot.example") as client:
        return await client.post(path, content=body,
                                 headers={"X-Razorpay-Signature": signature,
                                          "Content-Type": "application/json"})


def _paid_event(link_id="plink_1", payment_id="pay_9", notes=None):
    return {"event": "payment_link.paid", "payload": {
        "payment_link": {"entity": {"id": link_id, "status": "paid",
                                    "notes": notes or {"order_number": NUMBER}}},
        "payment": {"entity": {"id": payment_id, "status": "captured"}}}}


# --- making the link --------------------------------------------------------------
async def test_a_dst_order_gets_a_razorpay_link_for_its_total(
        session, customer, outlet, dst, quiet, razorpay):
    recorded, answers = razorpay
    answers[("POST", "/payment_links")] = {"id": "plink_new", "short_url": "https://rzp.io/i/a"}
    order = await _order(session, customer, outlet, stripe_session_id=None,
                         payment_status=PaymentStatus.NOT_STARTED)

    url = await payments.create_payment_link(session, order, customer)

    assert url == "https://rzp.io/i/a"
    assert order.stripe_session_id == "plink_new"
    assert order.checkout_url == "https://rzp.io/i/a"
    assert order.payment_status == PaymentStatus.LINK_SENT
    body = recorded[0][2]
    assert body["amount"] == 56290 and body["currency"] == "INR"
    assert body["notes"]["order_number"] == NUMBER
    assert quiet, "the payment link went out on WhatsApp"


async def test_shero_us_still_makes_its_link_with_stripe(session, customer, outlet, quiet,
                                                         monkeypatch):
    made = []

    async def create(**kw):
        made.append(kw)
        return {"id": "cs_test_1", "url": "https://checkout.stripe.com/x",
                "expires_at": datetime.now(timezone.utc) + timedelta(minutes=30)}

    async def never(*_a, **_k):
        raise AssertionError("Razorpay must not be used for Shero US")
    monkeypatch.setattr(stripe_checkout, "create_checkout_session", create)
    monkeypatch.setattr(rzp, "create_checkout_session", never)
    order = await _order(session, customer, outlet, order_number="SHO-261116-ABCDE",
                         currency="USD", stripe_session_id=None,
                         payment_status=PaymentStatus.NOT_STARTED)

    assert payment_gateway.current() is stripe_checkout
    await payments.create_payment_link(session, order, customer)
    assert order.stripe_session_id == "cs_test_1" and made


# --- the webhook ------------------------------------------------------------------
async def test_the_paid_webhook_confirms_the_order_once(session, customer, outlet, dst, quiet):
    order = await _order(session, customer, outlet)

    first = await _post_webhook(session, _paid_event())
    assert first.status_code == 200 and first.json()["paid"] is True
    assert order.payment_status == PaymentStatus.PAID
    assert order.stripe_payment_intent_id == "pay_9"
    assert order.stage == OrderStage.PAID_SLOT_BOOKED

    again = await _post_webhook(session, _paid_event())      # Razorpay retries
    assert again.status_code == 200 and again.json()["paid"] is False
    # One confirmation to the customer, then "being prepared" (DST India),
    # however often Razorpay calls.
    assert [name for name, _ in quiet] == ["payment_confirmed_v4", "order_preparing"]


async def test_an_order_is_found_by_its_notes_when_the_link_is_an_older_one(
        session, customer, outlet, dst, quiet):
    order = await _order(session, customer, outlet, stripe_session_id="plink_newer")
    response = await _post_webhook(session, _paid_event(link_id="plink_older"))
    assert response.json()["paid"] is True
    assert order.payment_status == PaymentStatus.PAID


async def test_a_bad_signature_is_refused_and_changes_nothing(session, customer, outlet, dst,
                                                            quiet):
    order = await _order(session, customer, outlet)
    response = await _post_webhook(session, _paid_event(), secret="not_the_secret")
    assert response.status_code == 401
    assert order.payment_status == PaymentStatus.LINK_SENT


async def test_shero_us_refuses_razorpay_webhooks(session, customer, outlet, quiet):
    order = await _order(session, customer, outlet, order_number="SHO-261116-ABCDE")
    response = await _post_webhook(session, _paid_event(), path="/webhooks/razorpay")
    assert response.status_code == 401
    assert order.payment_status == PaymentStatus.LINK_SENT


async def test_other_events_and_unknown_links_are_ignored(session, customer, outlet, dst, quiet):
    await _order(session, customer, outlet)
    other = await _post_webhook(session, {"event": "payment.authorized", "payload": {}})
    assert other.json()["status"] == "ignored"
    unknown = await _post_webhook(session, _paid_event(link_id="plink_x",
                                                       notes={"order_number": "DST-000000-ZZZZZ"}))
    assert unknown.json() == {"status": "ignored", "reason": "order not found"}


async def test_an_expired_link_is_checked_with_razorpay_before_the_slot_goes(
        session, customer, outlet, dst, quiet, razorpay):
    _, answers = razorpay
    answers[("GET", "/payment_links/plink_1")] = {"status": "expired", "payments": None}
    order = await _order(session, customer, outlet)
    response = await _post_webhook(session, {"event": "payment_link.expired", "payload": {
        "payment_link": {"entity": {"id": "plink_1", "notes": {"order_number": NUMBER}}}}})
    assert response.json()["outcome"] == "expired"
    assert order.payment_status == PaymentStatus.EXPIRED


# --- the signed return to /pay/success ----------------------------------------------
def _return_query(secret="key_secret_xyz", status="paid") -> dict:
    params = {"razorpay_payment_id": "pay_9", "razorpay_payment_link_id": "plink_1",
              "razorpay_payment_link_reference_id": f"{NUMBER}-1",
              "razorpay_payment_link_status": status}
    message = "|".join(params[k] for k in rzp.CALLBACK_FIELDS)
    params["razorpay_signature"] = hmac.new(secret.encode(), message.encode(),
                                            hashlib.sha256).hexdigest()
    return params


async def _get(session, path, params=None):
    transport = httpx.ASGITransport(app=_app(session))
    async with httpx.AsyncClient(transport=transport, base_url="http://bot.example") as client:
        return await client.get(path, params=params)


async def test_the_signed_return_confirms_a_paid_order(session, customer, outlet, dst, quiet):
    order = await _order(session, customer, outlet)
    page = await _get(session, f"/dst/pay/success/{NUMBER}", _return_query())
    assert page.status_code == 200 and "Payment received" in page.text
    assert order.payment_status == PaymentStatus.PAID
    assert order.stripe_payment_intent_id == "pay_9"
    # The webhook landing afterwards changes nothing.
    assert (await _post_webhook(session, _paid_event())).json()["paid"] is False


async def test_an_unsigned_return_confirms_nothing(session, customer, outlet, dst, quiet):
    order = await _order(session, customer, outlet)
    page = await _get(session, f"/dst/pay/success/{NUMBER}", _return_query(secret="forged"))
    assert page.status_code == 200
    assert order.payment_status == PaymentStatus.LINK_SENT


# --- the mock checkout, in DST India's terms ------------------------------------------
async def test_without_keys_the_test_page_stays_in_dst_india(session, customer, outlet, dst,
                                                             quiet, monkeypatch):
    monkeypatch.setattr(dst, "razorpay_key_id", "")
    await _order(session, customer, outlet, stripe_session_id=f"mock_{NUMBER}")
    page = await _get(session, f"/dst/pay/mock/{NUMBER}")
    assert page.status_code == 200
    assert "Razorpay" in page.text and "562.90 INR" in page.text
    assert f"action='/dst/pay/mock/{NUMBER}/paid'" in page.text


# --- the expiry job and refunds -----------------------------------------------------
async def test_a_lapsed_link_razorpay_says_was_paid_is_confirmed(session, customer, outlet, dst,
                                                                quiet, razorpay):
    _, answers = razorpay
    answers[("GET", "/payment_links/plink_1")] = {
        "status": "paid", "payments": [{"payment_id": "pay_late", "status": "captured"}]}
    order = await _order(session, customer, outlet,
                         payment_link_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    assert await payments.settle_lapsed_link(session, order, customer) == "paid"
    assert order.payment_status == PaymentStatus.PAID
    assert order.stripe_payment_intent_id == "pay_late"


async def test_an_open_lapsed_link_is_cancelled_at_razorpay_first(session, customer, outlet,
                                                                 dst, quiet, monkeypatch):
    calls, states = [], [{"status": "created", "payments": None},
                         {"status": "cancelled", "payments": None}]

    async def api(method, path, *, json_body=None, params=None):
        calls.append((method, path))
        return states.pop(0) if method == "GET" else {}
    monkeypatch.setattr(rzp, "_api", api)
    order = await _order(session, customer, outlet,
                         payment_link_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    assert await payments.settle_lapsed_link(session, order, customer) == "expired"
    assert ("POST", "/payment_links/plink_1/cancel") in calls
    assert order.payment_status == PaymentStatus.EXPIRED


async def test_a_refund_goes_back_through_razorpay(session, customer, outlet, dst, quiet,
                                                   razorpay):
    recorded, answers = razorpay
    answers[("POST", "/payments/pay_9/refund")] = {"id": "rfnd_1", "amount": 56290,
                                                  "status": "processed"}
    order = await _order(session, customer, outlet, stripe_payment_intent_id="pay_9",
                         payment_status=PaymentStatus.PAID, stage=OrderStage.PAID_SLOT_BOOKED)
    assert await payments.refund_order(session, order, customer)
    assert order.payment_status == PaymentStatus.REFUNDED
    assert order.stripe_refund_id == "rfnd_1"
    assert order.refund_amount == Decimal("562.90")
    assert ("POST", "/payments/pay_9/refund", {}) in recorded
    stored = (await session.execute(select(Order).where(Order.order_number == NUMBER))).scalar_one()
    assert stored.stage == OrderStage.REFUNDED
