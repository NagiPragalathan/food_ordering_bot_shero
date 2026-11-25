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
    assert page.status_code == 200 and "data-sft-test" in page.text
    assert "Pay $99 (test)" in page.text and f"/sft/enrol/{lead.pay_token}/test-paid" in page.text
    assert lead.link_clicked_at is not None


async def test_the_review_page_shows_the_course_and_the_order(session, cfg, rec, pages):
    lead = await _wants_to_pay(session, cfg)
    text = pages.get(f"/sft/enrol/{lead.pay_token}").text
    assert "Hi Priya" in text and "Shero Food Training (SFT)" in text
    for part in ("Your 5-day plan", "Meet Shero Food Technology", "Order execution and the daily",
                 "What you'll learn", "Allergens", "How certification works", "Order summary",
                 "Total due today", "The course brochure", "data-sft-summary"):
        assert part in text, part
    assert "••• 0001" in text                          # their number, masked


async def test_the_review_page_shows_an_offer_as_a_saving(session, cfg, rec, pages):
    from app.db.models import SftOffer

    session.add(SftOffer(title="Launch", code="SFT20", kind="amount", value=Decimal("20"),
                         active=True))
    await session.flush()
    lead = await _wants_to_pay(session, cfg)
    await say(session, cfg, "SFT20")
    text = pages.get(f"/sft/enrol/{lead.pay_token}").text
    assert "data-sft-offer" in text and "SFT20" in text and "$20" in text
    assert "Pay $79 (test)" in text


async def test_pay_opens_stripe_and_a_test_lead_goes_back_to_review(session, cfg, rec, pages,
                                                                    monkeypatch):
    lead = await _wants_to_pay(session, cfg)
    # A test checkout has no Stripe step: back to the review page.
    page = pages.get(f"/sft/enrol/{lead.pay_token}/pay")
    assert page.status_code == 303 and page.headers["location"] == f"/sft/enrol/{lead.pay_token}"

    async def checkout(session, lead, cfg):
        return "https://checkout.stripe.com/c/pay/cs_live_1"

    monkeypatch.setattr(payments, "test_checkout", lambda lead, cfg: False)
    monkeypatch.setattr(payments, "checkout_url", checkout)
    review = pages.get(f"/sft/enrol/{lead.pay_token}").text
    assert f'action="/sft/enrol/{lead.pay_token}/pay"' in review and "Pay $99 securely" in review
    page = pages.get(f"/sft/enrol/{lead.pay_token}/pay")
    assert page.status_code == 303 and page.headers["location"].startswith("https://checkout.stripe")


async def test_test_payments_switch_decides_if_test_numbers_pay_for_real(session, monkeypatch):
    from app.db.models import SftLead
    from app.integrations.stripe_gw import checkout

    monkeypatch.setattr(checkout, "mock_mode", lambda: False)        # a real Stripe key
    tester, customer = SftLead(whatsapp_number=NUMBER), SftLead(whatsapp_number="15550009999")
    cfg = await config.save(session, {"mode": "test", "test_numbers": NUMBER}, "t")
    assert cfg.test_payments                                           # on unless switched off
    assert payments.test_checkout(tester, cfg)                         # no charge
    cfg = await config.save(session, {"mode": "test", "test_numbers": NUMBER,
                                      "test_payments": False}, "t")
    assert not payments.test_checkout(tester, cfg)                     # the real Stripe page
    cfg = await config.save(session, {"mode": "live", "test_numbers": NUMBER}, "t")
    assert cfg.test_payments and not payments.test_checkout(tester, cfg)
    assert not payments.test_checkout(customer, cfg)                   # live: everyone pays


async def test_the_test_payment_enrols_and_sends_the_course(session, rec, pages):
    cfg = await config.save(session, {"mode": "live", "course_url": COURSE}, "t")
    lead = await _wants_to_pay(session, cfg)
    rec.clear()
    page = pages.post(f"/sft/enrol/{lead.pay_token}/test-paid")
    assert page.status_code == 200 and "data-sft-enrolled" in page.text
    assert "You're enrolled, Priya!" in page.text and "Test payment: nobody was charged" in page.text
    assert "Open your course" in page.text and "Download your receipt (PDF)" in page.text
    assert lead.stage == c.ENROLLED and lead.paid_at and lead.test_payment
    assert lead.amount_paid == Decimal("99")
    kind, body, (url, label) = rec.last()
    assert kind == "link" and body.startswith("Congratulations, Priya!") and "$99" in body
    assert url == COURSE and label == c.COURSE_BUTTON


async def test_paying_sends_the_receipt_pdf_after_the_confirmation(session, rec, pages):
    from app.sft import receipt

    cfg = await config.save(session, {"mode": "live", "course_url": COURSE}, "t")
    lead = await _wants_to_pay(session, cfg)
    rec.clear()
    pages.post(f"/sft/enrol/{lead.pay_token}/test-paid")
    assert rec.kinds()[-2:] == ["link", "document"]            # confirmation, then receipt
    (kind, name, url), = rec.receipts()
    assert name == receipt.filename(lead) and name.startswith("SFT-Enrollment-Receipt-SFT-")
    assert url.endswith(f"/sft/receipt/{lead.pay_token}/{name}")
    # The PDF was saved before WhatsApp was told, and the link serves it.
    pdf = pages.get(f"/sft/receipt/{lead.pay_token}/{name}")
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF-") and len(pdf.content) > 1500


async def test_a_receipt_needs_the_right_token_and_a_payment(session, cfg, rec, pages):
    from app.sft import receipt

    lead = await _wants_to_pay(session, cfg)
    name = receipt.filename(lead)
    assert pages.get(f"/sft/receipt/{lead.pay_token}/{name}").status_code == 404   # unpaid
    assert pages.get(f"/sft/receipt/nope-nope-nope/{name}").status_code == 404
    await payments.mark_paid(session, lead, cfg, amount=Decimal("99"), currency="usd")
    name = receipt.filename(lead)
    assert pages.get(f"/sft/receipt/{lead.pay_token}/{name}").status_code == 200
    assert pages.get(f"/sft/receipt/{lead.pay_token}/../../etc.pdf").status_code == 404


async def test_the_receipt_pdf_shows_the_offer_and_a_test_payment(session, cfg):
    from app.db.models import SftLead
    from app.sft import receipt

    lead = SftLead(whatsapp_number=NUMBER, name="Priya Raman", pay_token="tok_abcdefgh",
                   amount_paid=Decimal("79"), currency="USD", offer_code="SFT20",
                   test_payment=True)
    session.add(lead)
    await session.flush()
    lead.paid_at = leads.now()
    pdf = receipt.build_pdf(lead, cfg, offer_title="Launch")
    assert pdf.startswith(b"%PDF-")
    assert receipt.number(lead).startswith("SFT-") and len(receipt.number(lead)) == 19


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


# --- Paying outside WhatsApp's 24-hour window: templates ----------------------------------
def _approve(monkeypatch, answer: bool):
    from app.integrations.gallabox import template_status

    async def approved(name):
        return answer

    monkeypatch.setattr(template_status, "is_approved", approved)
    from app.sft import sending
    sending.clear_cache()


async def _quiet(session, cfg, lead):
    from datetime import timedelta

    lead.last_inbound_at = leads.now() - timedelta(hours=30)       # the window has closed


async def test_paid_outside_the_window_goes_as_sft_enrolled(session, rec, monkeypatch):
    from app.sft import receipt

    _approve(monkeypatch, True)
    cfg = await config.save(session, {"mode": "live", "course_url": COURSE}, "t")
    lead = await _wants_to_pay(session, cfg)
    await _quiet(session, cfg, lead)
    rec.clear()
    await payments.mark_paid(session, lead, cfg, amount=Decimal("99"), currency="usd")
    assert rec.sent == [("template", "sft_enrolled", ("Priya", "$99", receipt.number(lead)))]
    assert receipt.stored(lead.pay_token, receipt.filename(lead)) is not None
    assert not lead.needs_attention                       # it reached them; the tap gets the rest
    # Their tap on Get my course: the course and the receipt PDF.
    rec.clear()
    await say(session, cfg, "Get my course")
    assert rec.kinds() == ["link", "document"]
    assert rec.sent[0][2][0] == COURSE and rec.receipts()


async def test_paid_outside_the_window_unapproved_tells_the_team(session, rec, monkeypatch):
    _approve(monkeypatch, False)
    cfg = await config.save(session, {"mode": "live", "course_url": COURSE}, "t")
    lead = await _wants_to_pay(session, cfg)
    await _quiet(session, cfg, lead)
    rec.clear()
    await payments.mark_paid(session, lead, cfg, amount=Decimal("99"), currency="usd")
    assert rec.sent == []
    assert lead.needs_attention and lead.attention_stage == "course_link"
    from sqlalchemy import select

    from app.db.models import SftEvent

    task = (await session.execute(select(SftEvent).where(
        SftEvent.lead_id == lead.id, SftEvent.kind == "attention"))).scalars().all()[-1]
    assert "sft_enrolled not approved" in task.detail


async def test_a_failed_payment_outside_the_window_goes_as_sft_payment_failed(session, cfg, rec,
                                                                             monkeypatch):
    _approve(monkeypatch, True)
    lead = await _wants_to_pay(session, cfg)
    await _quiet(session, cfg, lead)
    rec.clear()
    await payments.handle_stripe(session, "payment_intent.payment_failed",
                                 {"metadata": {"kind": "sft", "sft_token": lead.pay_token}})
    assert rec.sent == [("template", "sft_payment_failed", ("Priya",))]
    rec.clear()
    await say(session, cfg, "Complete enrollment")        # its quick reply: the checkout again
    assert rec.last()[0] == "link" and "/sft/enrol/" in rec.last()[2][0]


def test_every_template_sft_needs_is_listed():
    names = [t.name for t in c.TEMPLATES]
    assert names == ["sft_followup_1", "sft_followup_2", "sft_followup_3", "sft_payment_reminder",
                     "sft_course_check", "sft_nurture", "sft_team_alert", "sft_enrolled",
                     "sft_payment_failed"]
    for spec in c.TEMPLATES:
        assert not spec.sample_body.rstrip(".").endswith("}}"), spec.name   # Meta's rule
