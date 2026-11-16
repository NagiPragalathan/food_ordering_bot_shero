"""Razorpay Payment Links (integrations/razorpay_gw/checkout.py): DST India's
payments. Nothing here reaches Razorpay - `_api` is replaced."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core import tenants
from app.core.config import settings_for
from app.core.exceptions import IntegrationError, PaymentError
from app.integrations import payment_gateway
from app.integrations.razorpay_gw import checkout as rzp
from app.integrations.stripe_gw import checkout as stripe_checkout

ITEMS = [{"retailer_id": "SAMBAR-1", "name": "Drumstick Sambar", "quantity": 2,
          "unit_price": "249.00", "line_total": "498.00"}]


@pytest.fixture
def dst(monkeypatch):
    """DST India with test keys, its pages at bot.example/dst."""
    own = settings_for(tenants.DST)
    monkeypatch.setattr(own, "razorpay_key_id", "rzp_test_abc123")
    monkeypatch.setattr(own, "razorpay_key_secret", "key_secret_xyz")
    monkeypatch.setattr(own, "razorpay_webhook_secret", "hook_secret")
    monkeypatch.setattr(own, "public_base_url", "https://bot.example/dst")
    monkeypatch.setattr(own, "payment_link_ttl_minutes", 30)
    with tenants.use(tenants.DST):
        yield own


@pytest.fixture
def calls(monkeypatch):
    """Razorpay answers from a script; every call is recorded."""
    recorded: list[tuple] = []
    answers: dict = {}

    async def api(method, path, *, json_body=None, params=None):
        recorded.append((method, path, json_body))
        answer = answers.get((method, path))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(rzp, "_api", api)
    recorded_answers = answers
    return recorded, recorded_answers


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --- which gateway ----------------------------------------------------------------
def test_shero_us_pays_through_stripe_and_dst_india_through_razorpay():
    assert payment_gateway.current() is stripe_checkout
    assert payment_gateway.name() == "Stripe"
    with tenants.use(tenants.DST):
        assert payment_gateway.current() is rzp
        assert payment_gateway.name() == "Razorpay"


def test_test_mode_follows_the_key(dst, monkeypatch):
    assert payment_gateway.test_mode()
    monkeypatch.setattr(dst, "razorpay_key_id", "rzp_live_abc")
    assert not payment_gateway.test_mode()


# --- the link -----------------------------------------------------------------------
def test_the_link_charges_the_bill_in_paise_and_comes_back_to_us(dst):
    now = datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc)
    expires = now + timedelta(minutes=30)
    body = rzp.build_link(order_id="o-1", order_number="DST-261116-ABCDE", items=ITEMS,
                          delivery_fee=Decimal("40.00"), taxes_and_fees=Decimal("24.90"),
                          currency="INR", customer_email=None,
                          customer_phone="919840012345", expires_at=expires,
                          discount=Decimal("50.00"), discount_label="Coupon DST50", now=now)
    assert body["amount"] == 51290                      # 498 + 40 + 24.90 - 50, in paise
    assert body["currency"] == "INR"
    assert body["expire_by"] == int(expires.timestamp()) + 60
    assert body["reference_id"].startswith("DST-261116-ABCDE-")
    assert len(body["reference_id"]) <= 40
    assert body["notes"] == {"order_id": "o-1", "order_number": "DST-261116-ABCDE"}
    assert body["callback_url"] == "https://bot.example/dst/pay/success/DST-261116-ABCDE"
    assert body["callback_method"] == "get"
    assert body["notify"] == {"sms": False, "email": False}
    assert body["customer"] == {"contact": "+919840012345"}
    assert "₹498.00" in body["description"] and "-₹50.00" in body["description"]


def test_each_attempt_gets_its_own_reference(dst):
    first = datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc)
    kw = dict(order_id="o-1", order_number="DST-261116-ABCDE", items=ITEMS,
              delivery_fee=Decimal("0"), taxes_and_fees=Decimal("0"), currency="INR",
              customer_email=None, customer_phone="", expires_at=first)
    one = rzp.build_link(**kw, now=first)["reference_id"]
    two = rzp.build_link(**kw, now=first + timedelta(minutes=31))["reference_id"]
    assert one != two


def test_a_bill_of_nothing_is_refused(dst):
    with pytest.raises(PaymentError):
        rzp.build_link(order_id="o", order_number="DST-261116-ABCDE", items=ITEMS,
                       delivery_fee=Decimal("0"), taxes_and_fees=Decimal("0"), currency="INR",
                       customer_email=None, customer_phone="",
                       expires_at=datetime.now(timezone.utc), discount=Decimal("498"))


async def test_without_keys_the_link_is_a_test_page(dst, monkeypatch, calls):
    monkeypatch.setattr(dst, "razorpay_key_id", "")
    recorded, _ = calls
    result = await rzp.create_checkout_session(
        order_id="o-1", order_number="DST-261116-ABCDE", items=ITEMS,
        delivery_fee=Decimal("40"), taxes_and_fees=Decimal("0"))
    assert result["id"] == "mock_DST-261116-ABCDE"
    assert result["url"] == "https://bot.example/dst/pay/mock/DST-261116-ABCDE"
    assert recorded == []                                # Razorpay never asked


async def test_with_keys_the_link_is_razorpays_and_lives_at_least_16_minutes(dst, calls):
    recorded, answers = calls
    answers[("POST", "/payment_links")] = {"id": "plink_1", "short_url": "https://rzp.io/i/x"}
    before = datetime.now(timezone.utc)
    result = await rzp.create_checkout_session(
        order_id="o-1", order_number="DST-261116-ABCDE", items=ITEMS,
        delivery_fee=Decimal("40"), taxes_and_fees=Decimal("0"), ttl_minutes=5,
        customer_phone="919840012345")
    assert result["id"] == "plink_1" and result["url"] == "https://rzp.io/i/x"
    assert result["expires_at"] >= before + timedelta(minutes=16)
    method, path, body = recorded[0]
    assert (method, path) == ("POST", "/payment_links")
    assert body["amount"] == 53800
    assert body["expire_by"] >= int((before + timedelta(minutes=16)).timestamp())


async def test_a_refused_link_is_a_payment_error(dst, calls):
    _, answers = calls
    answers[("POST", "/payment_links")] = IntegrationError("razorpay", "bad", status_code=400)
    with pytest.raises(PaymentError):
        await rzp.create_checkout_session(
            order_id="o-1", order_number="DST-261116-ABCDE", items=ITEMS,
            delivery_fee=Decimal("0"), taxes_and_fees=Decimal("0"))


# --- the link's state, shaped like Stripe's ------------------------------------------
async def test_a_paid_link_reads_as_a_paid_session(dst, calls):
    _, answers = calls
    answers[("GET", "/payment_links/plink_1")] = {
        "status": "paid", "payments": [{"payment_id": "pay_9", "status": "captured"}]}
    assert await rzp.session_state("plink_1") == {
        "status": "complete", "payment_status": "paid", "payment_intent": "pay_9"}


@pytest.mark.parametrize("status,shape", [("created", "open"), ("partially_paid", "open"),
                                          ("expired", "expired"), ("cancelled", "expired")])
async def test_unpaid_links_read_as_open_or_expired(dst, calls, status, shape):
    _, answers = calls
    answers[("GET", "/payment_links/plink_1")] = {"status": status, "payments": None}
    state = await rzp.session_state("plink_1")
    assert state["status"] == shape and state["payment_status"] == "unpaid"


async def test_a_mock_link_has_no_state_and_an_unreachable_razorpay_is_an_error(dst, calls):
    _, answers = calls
    assert await rzp.session_state("mock_DST-261116-ABCDE") is None
    answers[("GET", "/payment_links/plink_1")] = IntegrationError("razorpay", "down")
    with pytest.raises(PaymentError):
        await rzp.session_state("plink_1")


async def test_cancelling_a_link_that_cannot_be_cancelled_is_not_an_error(dst, calls):
    recorded, answers = calls
    answers[("POST", "/payment_links/plink_1/cancel")] = IntegrationError(
        "razorpay", "already paid", status_code=400)
    await rzp.expire_session("plink_1")
    await rzp.expire_session("mock_DST-261116-ABCDE")
    assert [c[1] for c in recorded] == ["/payment_links/plink_1/cancel"]


async def test_a_refund_is_asked_in_paise_and_reported_in_rupees(dst, calls):
    recorded, answers = calls
    answers[("POST", "/payments/pay_9/refund")] = {"id": "rfnd_1", "amount": 24950,
                                                  "status": "processed"}
    refund = await rzp.create_refund("pay_9", Decimal("249.50"))
    assert refund == {"id": "rfnd_1", "amount": Decimal("249.50"), "status": "processed"}
    assert recorded[0][2] == {"amount": 24950}
    await rzp.create_refund("pay_9")                       # in full: no amount sent
    assert recorded[1][2] == {}


# --- signatures ---------------------------------------------------------------------
def test_a_webhook_is_read_only_with_its_signature(dst):
    body = json.dumps({"event": "payment_link.paid"}).encode()
    assert rzp.verify_webhook(body, _sign("hook_secret", body))["event"] == "payment_link.paid"
    with pytest.raises(PaymentError):
        rzp.verify_webhook(body, _sign("another_secret", body))
    with pytest.raises(PaymentError):
        rzp.verify_webhook(body, None)


def test_without_a_webhook_secret_nothing_is_accepted(dst, monkeypatch):
    monkeypatch.setattr(dst, "razorpay_webhook_secret", "")
    body = b"{}"
    with pytest.raises(PaymentError):
        rzp.verify_webhook(body, _sign("", body))


def _return(status="paid", secret="key_secret_xyz", **changes):
    params = {"razorpay_payment_id": "pay_9", "razorpay_payment_link_id": "plink_1",
              "razorpay_payment_link_reference_id": "DST-261116-ABCDE-1",
              "razorpay_payment_link_status": status}
    message = "|".join(params[k] for k in ("razorpay_payment_link_id",
                                           "razorpay_payment_link_reference_id",
                                           "razorpay_payment_link_status",
                                           "razorpay_payment_id"))
    params["razorpay_signature"] = _sign(secret, message.encode())
    params.update(changes)
    return params


def test_the_return_after_paying_is_trusted_only_when_signed_with_the_key_secret(dst):
    assert rzp.verify_callback(_return())
    assert not rzp.verify_callback(_return(secret="wrong"))
    assert not rzp.verify_callback(_return(razorpay_payment_link_status="created"))
    assert not rzp.verify_callback({"razorpay_signature": "x"})
