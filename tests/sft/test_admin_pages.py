"""The SFT admin's sections beyond the journey: payments, customers, the
leads board, nudges by flow, offers, the bot's wording, the CSV, the dates."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from app.db.models import SftOffer
from app.sft import config, leads, nudges, payments
from app.sft import content as c
from tests.sft.conftest import NUMBER, say
from tests.sft.test_admin import admin  # noqa: F401 - the fixture


async def _enrolled(session, cfg, number=NUMBER, name="Priya"):
    await say(session, cfg, "SFT", number=number)
    await say(session, cfg, name, number=number)
    lead = await leads.get(session, number)
    await payments.mark_paid(session, lead, cfg, amount=Decimal("99"), currency="usd",
                             payment_intent="pi_123")
    return lead


async def test_payments_shows_what_was_paid_and_who_is_waiting(admin, session, cfg, rec):
    await _enrolled(session, cfg)
    await say(session, cfg, "SFT", number="15551230002")
    await say(session, cfg, "Meena", number="15551230002")
    await say(session, cfg, button=c.B_ENROL, title="Enroll now", number="15551230002")
    page = admin.get("/admin/sft?tab=payments").text
    assert "Paid enrollments" in page and "$99" in page and "Priya" in page
    assert "dashboard.stripe.com/payments/pi_123" in page
    assert "Waiting to pay now" in page and "Meena" in page


async def test_customers_lists_the_students_and_their_course_access(admin, session, cfg, rec):
    lead = await _enrolled(session, cfg)
    page = admin.get("/admin/sft?tab=customers").text
    assert "Priya" in page and "Team to send access" in page        # no course link is set
    assert "Priya" in admin.get("/admin/sft?tab=customers&access=help").text
    await leads.resolve(session, lead, "sent the access", by="Ops")
    assert "Not asked yet" in admin.get("/admin/sft?tab=customers").text
    lead.access_confirmed_at = leads.now()
    assert "Opened the course" in admin.get("/admin/sft?tab=customers&access=opened").text
    assert "Priya" not in admin.get("/admin/sft?tab=customers&access=help").text


async def test_the_leads_board_shows_each_stage_and_the_flow(admin, session, cfg, rec):
    await say(session, cfg, "SFT")
    await say(session, cfg, "Priya")
    page = admin.get("/admin/sft?tab=leads&layout=board").text
    assert "data-sft-board" in page
    for stage in c.FUNNEL:
        assert f'data-stage="{stage}"' in page
    assert "Waiting for a goal" in page and "Next: Follow-up 1" in page
    listing = admin.get("/admin/sft?tab=leads").text
    assert "Waiting for a goal" in listing


async def test_a_custom_date_range_filters_the_leads(admin, session, cfg, rec):
    await say(session, cfg, "SFT")
    lead = await leads.get(session, NUMBER)
    lead.created_at = leads.now() - timedelta(days=40)
    await session.flush()
    assert "Priya" not in admin.get("/admin/sft?tab=leads&range=7").text
    old = admin.get(f"/admin/sft?tab=leads&range=custom&from="
                    f"{(leads.now() - timedelta(days=45)).date()}&to="
                    f"{(leads.now() - timedelta(days=35)).date()}").text
    assert "+15551230001" in old
    # The sidebar keeps the chosen dates on the other dated pages.
    assert "tab=payments&amp;range=custom" in old or "tab=payments&range=custom" in old


async def test_nudges_show_who_is_in_which_flow_and_what_was_sent(admin, session, rec):
    cfg = await config.save(session, {"mode": "live", "send_from_hour": 0,
                                      "send_until_hour": 24}, "t")       # any hour
    await say(session, cfg, "SFT")
    await say(session, cfg, "Priya")
    lead = await leads.get(session, NUMBER)
    page = admin.get("/admin/sft?tab=nudges&section=flows").text
    assert "In the journey" in page and "Priya" in page
    await nudges.one(session, lead, cfg, leads.now() + timedelta(hours=25))
    sent = admin.get("/admin/sft?tab=nudges&section=sent").text
    assert "sft_followup_1" in sent and "Follow-up 1 of 3" in sent
    rules = admin.get("/admin/sft?tab=nudges").text
    assert "Follow-up 1" in rules and "Payment link again" in rules and "Hand to the team" in rules


async def test_offers_are_created_edited_switched_and_deleted(admin, session, cfg):
    made = admin.post("/admin/sft/offers", data={
        "title": "Launch week", "code": "sft 20", "kind": "amount", "value": "20",
        "active": "on", "sources": ["Instagram"]})
    assert made.status_code == 303
    offer = (await session.execute(select(SftOffer))).scalar_one()
    assert offer.code == "SFT20" and offer.value == Decimal("20") and offer.sources == ["Instagram"]
    page = admin.get("/admin/sft?tab=offers").text
    assert "SFT20" in page and "Running" in page and "$79" in page

    admin.post("/admin/sft/offers", data={"offer_id": str(offer.id), "title": "Launch week",
                                          "code": "SFT20", "kind": "percent", "value": "10",
                                          "active": "on"})
    assert offer.kind == "percent" and offer.sources == []
    admin.post(f"/admin/sft/offers/{offer.id}/toggle")
    assert not offer.active
    admin.post(f"/admin/sft/offers/{offer.id}/delete")
    assert (await session.execute(select(SftOffer))).scalar_one_or_none() is None


async def test_a_bad_offer_is_refused(admin, session, cfg):
    for data, why in (({"title": "", "value": "10"}, "name"),
                      ({"title": "x", "code": "SFT", "value": "10"}, "keywords"),
                      ({"title": "x", "kind": "percent", "value": "100"}, "less than 100"),
                      ({"title": "x", "kind": "price", "value": "120"}, "less than the fee"),
                      ({"title": "x", "value": "10", "starts_on": "2026-12-10",
                        "ends_on": "2026-12-01"}, "ends before")):
        answer = admin.post("/admin/sft/offers", data=data)
        assert why in answer.cookies.get("shero_flash", ""), why
    assert (await session.execute(select(SftOffer))).scalar_one_or_none() is None


async def test_the_bots_wording_is_edited_and_put_back(admin, session):
    page = admin.get("/admin/sft?tab=replies").text
    assert 'id="text-welcome"' in page and "What the bot says" in page
    admin.post("/admin/sft/texts", data={"key": "welcome", "text": "Hello {first}! {fee} only."})
    cfg = await config.load(session, fresh=True)
    assert cfg.texts["welcome"] == "Hello {first}! {fee} only."
    assert "Hello Priya! $99 only." in admin.get("/admin/sft?tab=journey").text
    bad = admin.post("/admin/sft/texts", data={"key": "welcome", "text": "Hi {price}"})
    assert "can't be filled in" in bad.cookies.get("shero_flash", "")
    admin.post("/admin/sft/texts", data={"key": "welcome", "action": "reset"})
    assert "welcome" not in (await config.load(session, fresh=True)).texts


async def test_check_a_message_says_who_answers_it(admin, session, cfg):
    page = admin.get("/admin/sft?tab=replies&number=15551230009&message=I+saw+SFT").text
    assert "SFT answers it." in page and "keyword" in page
    page = admin.get("/admin/sft?tab=replies&number=15551230009&message=order+food").text
    assert "The ordering bot answers it." in page


async def test_the_csv_has_the_leads_and_the_payments(admin, session, cfg, rec):
    await _enrolled(session, cfg)
    csv = admin.get("/admin/sft/export?what=payments&range=all")
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv.headers["content-disposition"]
    lines = csv.text.strip().splitlines()
    assert lines[0].startswith("Name,WhatsApp") and "Priya" in lines[1] and "99" in lines[1]
    assert "+15551230001" in admin.get("/admin/sft/export?what=leads").text
