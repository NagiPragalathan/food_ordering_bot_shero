"""The Shero Training course platform (app/sft/platform.py): once a lead pays,
their account is made there (it emails the invite) and the bot sends them
the login. Nothing here reaches the network: platform._post is replaced."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import tenants
from app.core.config import settings_for
from app.db.session import get_session
from app.integrations.stripe_gw import checkout
from app.sft import config, leads, payments, platform, web
from app.sft import content as c
from tests.sft.conftest import NUMBER, say

SECRET = "whsec_test_platform"
LOGIN = "https://sft.sherosft.com/partner-login"


class Platform:
    """Stands in for api.sherosft.com: records each call, answers as told."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict]] = []
        self.answers: list[tuple[int, dict]] = []

    async def __call__(self, url, *, json, headers):
        self.calls.append((url, json, headers))
        return self.answers.pop(0) if self.answers else (200, {"ok": True, "id": "rec-1"})


@pytest.fixture
def api(monkeypatch):
    fake = Platform()
    monkeypatch.setattr(platform, "_post", fake)
    monkeypatch.setattr(settings_for(tenants.US), "sft_platform_webhook_secret", SECRET)
    return fake


@pytest.fixture
def pages(session):
    app = FastAPI()
    app.include_router(web.router)
    app.add_middleware(tenants.TenantPathMiddleware)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app, follow_redirects=False)


async def _wants_to_pay(session, cfg):
    await say(session, cfg, "SFT")
    await say(session, cfg, "Priya Raman")
    await say(session, cfg, button=c.B_ENROL, title="Enroll now")
    return await leads.get(session, NUMBER)


async def test_paying_makes_the_account_and_sends_the_login(session, cfg, rec, api):
    lead = await _wants_to_pay(session, cfg)
    lead.email = "Priya@Example.com"
    rec.clear()
    await payments.mark_paid(session, lead, cfg, amount=Decimal("99"), currency="usd",
                             payment_intent="pi_42")
    (url, sent, headers), = api.calls
    assert url == "https://api.sherosft.com/api/partner-payments/webhook"
    assert headers == {"x-webhook-secret": SECRET}
    assert sent == {"partner_name": "Priya Raman", "partner_email": "priya@example.com",
                    "payment_id": "pi_42", "payment_status": "paid", "amount": 99.0,
                    "partner_id": f"WA-{NUMBER}"}
    assert lead.platform_enrolled_at and lead.platform_id == "rec-1" and lead.login_sent_at
    kind, body, (link, label) = rec.last()
    assert kind == "link" and link == LOGIN and label == c.COURSE_LOGIN_BUTTON
    assert "Priya@Example.com" in body and "6-digit code" in body and "$99" in body
    assert len(label) <= 20 and rec.receipts()                      # the receipt PDF too


async def test_a_platform_hiccup_is_tried_again_then_the_login_goes(session, cfg, rec, api):
    lead = await _wants_to_pay(session, cfg)
    lead.email = "priya@example.com"
    api.answers = [(500, {"error": "Resend is down"})]
    rec.clear()
    await payments.mark_paid(session, lead, cfg, amount=Decimal("99"), currency="usd",
                             payment_intent="pi_43")
    assert not lead.platform_enrolled_at and lead.platform_attempts == 1
    assert "Resend is down" in lead.platform_error and lead.platform_next_try_at
    assert "setting up your course account" in rec.last()[1]
    assert await platform.run(session) == 0                         # not due yet
    lead.platform_next_try_at = leads.now() - timedelta(seconds=1)
    assert await platform.run(session) == 1
    assert lead.platform_enrolled_at and len(api.calls) == 2
    assert api.calls[1][1]["payment_id"] == "pi_43"                 # the same payment again
    assert rec.last()[2][0] == LOGIN


async def test_a_refused_secret_goes_to_the_team(session, cfg, rec, api):
    lead = await _wants_to_pay(session, cfg)
    lead.email = "priya@example.com"
    api.answers = [(401, {"error": "Invalid webhook secret"})]
    await payments.mark_paid(session, lead, cfg, amount=Decimal("99"), currency="usd",
                             payment_intent="pi_44")
    assert lead.needs_attention and lead.attention_stage == "course_account"
    assert "check it on SFT Settings" in lead.platform_error and not lead.platform_next_try_at


async def test_no_email_means_no_account_and_a_task(session, cfg, rec, api):
    lead = await _wants_to_pay(session, cfg)
    await payments.mark_paid(session, lead, cfg, amount=Decimal("99"), currency="usd",
                             payment_intent="pi_45")
    assert not api.calls and lead.attention_stage == "course_account"


async def test_test_payments_make_accounts_only_when_switched_on(session, rec, api):
    cfg = await config.save(session, {"mode": "live"}, "t")
    lead = await _wants_to_pay(session, cfg)
    lead.email = "priya@example.com"
    await payments.mark_paid(session, lead, cfg, amount=None, currency="usd", test=True)
    assert not api.calls and not platform.wanted(lead, cfg)
    cfg = await config.save(session, {"mode": "live", "platform_for_tests": True}, "t")
    assert await platform.enrol(session, lead, cfg) == platform.DONE
    assert api.calls[0][1]["payment_id"] == f"sft-test-{lead.pay_token}"


async def test_not_connected_keeps_the_course_link(session, rec):
    cfg = await config.save(session, {"mode": "live", "course_url": "https://learn.example"}, "t")
    lead = await _wants_to_pay(session, cfg)
    await payments.mark_paid(session, lead, cfg, amount=Decimal("99"), currency="usd")
    assert rec.last()[2][0] == "https://learn.example" and not lead.platform_enrolled_at


async def test_the_enrol_page_asks_for_the_email_first(session, cfg, rec, api, pages,
                                                       monkeypatch):
    lead = await _wants_to_pay(session, cfg)
    review = pages.get(f"/sft/enrol/{lead.pay_token}").text
    assert 'name="email"' in review and "required" in review and "your course login" in review
    assert pages.get(f"/sft/enrol/{lead.pay_token}/pay").headers["location"].endswith("#email")
    bad = pages.post(f"/sft/enrol/{lead.pay_token}/test-paid", data={"email": "nope"})
    assert bad.status_code == 400 and "data-sft-email-error" in bad.text and not lead.paid_at
    monkeypatch.setattr(checkout, "mock_mode", lambda: True)
    done = pages.post(f"/sft/enrol/{lead.pay_token}/test-paid",
                      data={"email": "Priya@Example.com"})
    assert done.status_code == 200 and lead.email == "priya@example.com" and lead.paid_at
    assert "data-sft-login" not in done.text                        # test: no account made


async def test_the_enrolled_page_shows_the_login(session, cfg, rec, api, pages):
    lead = await _wants_to_pay(session, cfg)
    lead.email = "priya@example.com"
    await payments.mark_paid(session, lead, cfg, amount=Decimal("99"), currency="usd",
                             payment_intent="pi_46")
    page = pages.get(f"/sft/enrol/{lead.pay_token}").text
    assert "data-sft-login" in page and LOGIN in page and "priya@example.com" in page


async def test_already_paid_gets_the_login_again(session, cfg, rec, api):
    lead = await _wants_to_pay(session, cfg)
    lead.email = "priya@example.com"
    await payments.mark_paid(session, lead, cfg, amount=Decimal("99"), currency="usd",
                             payment_intent="pi_47")
    rec.clear()
    await say(session, cfg, "SFT")
    assert rec.last()[2][0] == LOGIN and len(api.calls) == 1        # no second account


async def test_one_payment_confirmed_twice_at_once_enrols_once(session, cfg, rec, api):
    """Stripe's webhook and the return page together: one call to the platform."""
    import asyncio

    lead = await _wants_to_pay(session, cfg)
    lead.email = "priya@example.com"
    first, second = await asyncio.gather(
        payments.mark_paid(session, lead, cfg, amount=Decimal("99"), currency="usd",
                           payment_intent="pi_48"),
        payments.mark_paid(session, lead, cfg, amount=Decimal("99"), currency="usd",
                           payment_intent="pi_48"))
    assert (first, second) == (True, False) and len(api.calls) == 1
    lead.paid_at = None                     # as a second session's stale copy sees it
    assert await payments.mark_paid(session, lead, cfg, amount=Decimal("99"),
                                    currency="usd", payment_intent="pi_48") is False
    assert len(api.calls) == 1


async def test_a_payment_on_its_way_is_not_sent_again(session, cfg, rec, api, monkeypatch):
    lead = await _wants_to_pay(session, cfg)
    lead.email = "priya@example.com"
    lead.paid_at, lead.payment_intent_id = leads.now(), "pi_49"
    monkeypatch.setattr(platform, "_posting", {"pi_49"})
    assert await platform.enrol(session, lead, cfg) == platform.BUSY and not api.calls


async def test_check_connection_makes_nothing(api):
    cfg = config.SftConfig()
    api.answers = [(400, {"error": "partner_name, partner_email, and payment_id are required"})]
    ok, message = await platform.check(cfg)
    assert ok and "nothing was made" in message and api.calls[0][1] == {}
    api.answers = [(401, {"error": "Invalid webhook secret"})]
    assert (await platform.check(cfg))[0] is False


def test_a_login_page_must_be_https():
    with pytest.raises(ValueError):
        config.SftConfig(platform_login_url="http://sft.example/login")
    assert config.SftConfig(platform_login_url="https://x.example/login/").platform_login_url \
        == "https://x.example/login"
