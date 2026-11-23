"""Enrolling: the enroll link, the test checkout, Stripe's webhook, the course link."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import tenants
from app.db.session import get_session
from app.sft import config, leads, payments, web
from app.sft import content as c
from tests.sft.conftest import NUMBER, say

COURSE = "https://learn.shero.example/sft"


@pytest.fixture
def pages(session):
    app = FastAPI()
    app.include_router(web.router)
    app.add_middleware(tenants.TenantPathMiddleware)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app, follow_redirects=False)


async def _wants_to_pay(session, cfg):
    await say(session, cfg, "SFT")
    await say(session, cfg, "Priya")
    await say(session, cfg, "yes")       # not at the brochure yet: a prompt again
    await say(session, cfg, button=c.B_ENROL, title="Enroll now")
    return await leads.get(session, NUMBER)


async def test_without_stripe_the_link_opens_the_test_checkout(session, cfg, rec, pages):
    lead = await _wants_to_pay(session, cfg)
    page = pages.get(f"/sft/enrol/{lead.pay_token}")
    assert page.status_code == 200 and "Test payment" in page.text and "Pay $99 (test)" in page.text
    assert lead.link_clicked_at is not None


async def test_the_test_payment_enrols_and_sends_the_course(session, rec, pages):
    cfg = await config.save(session, {"mode": "live", "course_url": COURSE}, "t")
    lead = await _wants_to_pay(session, cfg)
    rec.clear()
    page = pages.post(f"/sft/enrol/{lead.pay_token}/test-paid")
    assert page.status_code == 200 and "Test payment done" in page.text
    assert lead.stage == c.ENROLLED and lead.paid_at and lead.test_payment
    assert lead.amount_paid == Decimal("99")
    kind, body, (url, label) = rec.last()
    assert kind == "link" and body.startswith("Congratulations, Priya!") and "$99" in body
    assert url == COURSE and label == c.COURSE_BUTTON


async def test_without_a_course_link_the_team_is_asked_to_send_access(session, cfg, rec, pages):
    lead = await _wants_to_pay(session, cfg)
    pages.post(f"/sft/enrol/{lead.pay_token}/test-paid")
    assert lead.needs_attention and lead.attention_stage == "course_link"
    assert "send your course access details" in rec.last()[1]


async def test_an_unknown_link_is_a_404(session, cfg, pages):
    assert pages.get("/sft/enrol/nope").status_code == 404


async def test_stripes_webhook_enrols_once(session, rec):
    cfg = await config.save(session, {"mode": "live", "course_url": COURSE}, "t")
    lead = await _wants_to_pay(session, cfg)
    completed = {"id": "cs_1", "object": "checkout.session", "payment_status": "paid",
                 "amount_total": 9900, "currency": "usd", "payment_intent": "pi_1",
                 "customer_details": {"email": "priya@example.com"},
                 "metadata": {"kind": "sft", "sft_token": lead.pay_token}}
    assert payments.is_sft(completed)
    await payments.handle_stripe(session, "checkout.session.completed", completed)
    assert lead.paid_at and not lead.test_payment and lead.payment_intent_id == "pi_1"
    assert lead.email == "priya@example.com"
    sent = len(rec.sent)
    await payments.handle_stripe(session, "checkout.session.completed", completed)
    assert len(rec.sent) == sent                                # a replay sends nothing


async def test_a_paid_lead_tapping_enrol_gets_the_course_again(session, rec):
    cfg = await config.save(session, {"mode": "live", "course_url": COURSE}, "t")
    lead = await _wants_to_pay(session, cfg)
    await payments.mark_paid(session, lead, cfg, amount=Decimal("99"), currency="usd")
    rec.clear()
    await say(session, cfg, button=c.B_ENROL, title="Enroll now")
    kind, body, (url, _) = rec.last()
    assert body.startswith("You're already enrolled") and url == COURSE


async def test_a_failed_payment_offers_the_link_again(session, cfg, rec):
    lead = await _wants_to_pay(session, cfg)
    rec.clear()
    await payments.handle_stripe(session, "payment_intent.payment_failed",
                                 {"metadata": {"kind": "sft", "sft_token": lead.pay_token}})
    kind, body, (url, label) = rec.last()
    assert "didn't go through" in body and label == "Try again"


async def test_the_stripe_webhook_hands_sft_checkouts_over(session, monkeypatch):
    """webhooks_stripe gives a kind=sft object to SFT, not to the orders."""
    from app.api.routes import webhooks_stripe

    seen = []

    async def sft(session, event_type, obj):
        seen.append(event_type)
        return {"status": "ok", "sft": True}

    monkeypatch.setattr(payments, "handle_stripe", sft)
    monkeypatch.setattr(webhooks_stripe, "verify_webhook", lambda p, s: {
        "type": "checkout.session.completed", "id": "evt",
        "data": {"object": {"metadata": {"kind": "sft", "sft_token": "x"}}}})

    class FakeRequest:
        async def body(self):
            return b"{}"

    result = await webhooks_stripe.stripe_webhook(FakeRequest(), session=session,
                                                  stripe_signature="sig")
    assert result["sft"] and seen == ["checkout.session.completed"]
