"""The admin's levers on the journey: offers (codes and launch prices), the
bot's wording (Bot replies), the nudges' switches, and the date ranges."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models import SftEvent, SftOffer
from app.sft import config, leads, nudges, offers, payments, periods, router, views
from app.sft import content as c
from tests.sft.conftest import NUMBER, event, say

NOON = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)       # 10:00 in New York


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(leads, "now", lambda: NOON)
    for module in ("app.sft.flow", "app.sft.nudges", "app.sft.sending", "app.sft.payments"):
        monkeypatch.setattr(f"{module}.now", lambda: NOON, raising=False)


async def _offer(session, **kw) -> SftOffer:
    offer = SftOffer(**{"title": "Launch", "code": "SFT20", "kind": "amount",
                        "value": Decimal("20"), "active": True, "sources": [], **kw})
    session.add(offer)
    await session.flush()
    return offer


# --- date ranges ----------------------------------------------------------------------
def test_presets_and_custom_ranges_are_in_the_local_timezone():
    now = datetime(2026, 11, 23, 3, 0, tzinfo=timezone.utc)        # still the 22nd in New York
    today = periods.resolve("today", tz="America/New_York", now=now)
    assert today.first == today.last == date(2026, 11, 22)
    assert today.start == datetime(2026, 11, 22, 5, 0, tzinfo=timezone.utc)
    week = periods.resolve("7", tz="America/New_York", now=now)
    assert week.first == date(2026, 11, 16) and week.last == date(2026, 11, 22)
    last_month = periods.resolve("last_month", tz="America/New_York", now=now)
    assert (last_month.first, last_month.last) == (date(2026, 10, 1), date(2026, 10, 31))
    custom = periods.resolve("custom", "2026-11-10", "2026-11-01", tz="America/New_York")
    assert (custom.first, custom.last) == (date(2026, 11, 1), date(2026, 11, 10))  # swapped
    assert custom.query() == {"range": "custom", "from": "2026-11-01", "to": "2026-11-10"}
    assert custom.has(datetime(2026, 11, 10, 23, 0, tzinfo=timezone.utc))
    assert not custom.has(datetime(2026, 11, 11, 6, 0, tzinfo=timezone.utc))
    assert periods.resolve("nonsense", tz="America/New_York").key == "30"
    assert periods.resolve("all", tz="America/New_York").start is None


# --- offers -------------------------------------------------------------------------
def test_an_offer_price_never_goes_below_stripes_minimum_or_above_the_fee():
    fee = Decimal("99")
    assert offers.price_after(fee, SftOffer(kind="percent", value=Decimal("20"))) == Decimal("79.20")
    assert offers.price_after(fee, SftOffer(kind="amount", value=Decimal("20"))) == Decimal("79")
    assert offers.price_after(fee, SftOffer(kind="price", value=Decimal("49"))) == Decimal("49")
    assert offers.price_after(fee, SftOffer(kind="amount", value=Decimal("500"))) == Decimal("0.50")
    assert offers.price_after(fee, SftOffer(kind="price", value=Decimal("150"))) == fee
    assert offers.find_code("hi, code sft20 please", ["SFT20"]) == "SFT20"
    assert offers.find_code("SFT200", ["SFT20"]) is None


async def test_a_code_as_the_first_message_starts_sft_at_the_offer_price(session, cfg, rec):
    await _offer(session)
    assert (await router.route(session, event("SFT20")))["status"] == "ok"   # shared number
    lead = await leads.get(session, NUMBER)
    assert lead.offer_code == "SFT20"
    assert any("Code *SFT20* applied" in body for _, body, _ in rec.sent)
    await say(session, cfg, "Priya")
    assert "$79 (was $99)" in rec.last()[1]                                 # the welcome


async def test_a_code_part_way_through_reprices_and_asks_again(session, cfg, rec):
    await _offer(session, code="LAUNCH", kind="price", value=Decimal("49"))
    await say(session, cfg, "SFT")
    await say(session, cfg, "Priya")
    rec.clear()
    await say(session, cfg, "launch")
    texts = [body for _, body, _ in rec.sent]
    assert "Your course fee is now *$49* (was $99)" in texts[0]
    assert rec.buttons() == ["Start food business", "Improve my skills"]        # asked again
    lead = await leads.get(session, NUMBER)
    assert not lead.with_team
    await say(session, cfg, button=c.B_BUSINESS, title="Start food business")
    await say(session, cfg, button=c.B_DETAILS, title="View course details")
    assert "$49 (was $99)" in rec.last()[1]
    await say(session, cfg, button=c.B_ENROL, title="Yes, enroll me")
    kind, body, (url, label) = rec.last()
    assert "$49 (was $99)" in body and label == "Pay $49 to enroll"


async def test_an_automatic_offer_covers_its_sources_only(session, cfg, rec):
    await _offer(session, code=None, kind="percent", value=Decimal("50"), sources=["Instagram"])
    await say(session, cfg, "SFT IG")
    await say(session, cfg, "Priya")
    assert "$49.50 (was $99)" in rec.last()[1]
    lead = await leads.get(session, NUMBER)
    other = await leads.create(session, "15551239999", source="WhatsApp")
    assert (await offers.price(session, other, cfg)).amount == Decimal("99")
    assert (await offers.price(session, lead, cfg)).amount == Decimal("49.50")


async def test_an_offer_out_of_dates_off_or_used_up_gives_nothing(session, cfg, rec):
    day = offers.today(cfg)
    await _offer(session, code="OLD", ends_on=day - timedelta(days=1))
    await _offer(session, code="SOON", starts_on=day + timedelta(days=1))
    await _offer(session, code="OFF", active=False)
    used = await _offer(session, code="ONE", usage_limit=1)
    paid = await leads.create(session, "15551238888", source="WhatsApp")
    paid.offer_id, paid.paid_at, paid.amount_paid = used.id, leads.now(), Decimal("79")
    for i, code in enumerate(("OLD", "SOON", "OFF", "ONE")):
        lead = await leads.create(session, f"155512300{i}{i}")
        assert await offers.from_message(session, lead, code, cfg) is None, code
    assert await router.route(session, event("OLD")) is None


async def test_the_checkout_charges_the_offer_price_and_records_it(session, cfg, rec, monkeypatch):
    from app.integrations.stripe_gw import checkout

    offer = await _offer(session)
    made = {}

    class Session:
        id, url = "cs_test_1", "https://checkout.stripe.test/1"

    async def create(params, ref):
        made.update(params)
        return Session()

    monkeypatch.setattr(checkout, "_create", create)
    monkeypatch.setattr(checkout, "_apply_api_key", lambda: None)
    await say(session, cfg, "SFT")
    await say(session, cfg, "SFT20")
    lead = await leads.get(session, NUMBER)
    await payments.checkout_url(session, lead, cfg)
    assert made["line_items"][0]["price_data"]["unit_amount"] == 7900
    assert made["metadata"]["offer_code"] == "SFT20" and lead.checkout_amount == Decimal("79")
    # A better price later opens a new checkout rather than reusing the old one.
    offer.value = Decimal("30")
    made.clear()
    await payments.checkout_url(session, lead, cfg)
    assert made["line_items"][0]["price_data"]["unit_amount"] == 6900
    await payments.mark_paid(session, lead, cfg, amount=None, currency="usd", test=True)
    assert lead.amount_paid == Decimal("69") and lead.offer_id == offer.id


# --- the bot's wording -------------------------------------------------------------------
async def test_an_edited_message_is_what_the_bot_says(session, rec):
    cfg = await config.save(session, {"mode": "live", "texts": {
        "welcome": "Hey {first}! SFT costs {fee}. Pick one:"}}, "t")
    await say(session, cfg, "SFT")
    await say(session, cfg, "Priya")
    assert rec.last()[1] == "Hey Priya! SFT costs $99. Pick one:"


def test_an_edit_with_a_placeholder_it_cant_fill_is_refused():
    assert c.check_text("welcome", "Hi {first}, it is {fee}") is None
    assert "{price}" in c.check_text("welcome", "Hi {first}, it is {price}")
    assert "not closed" in c.check_text("welcome", "Hi {first")
    assert c.check_text("welcome", "x" * 1100).startswith("at most")
    broken = config.SftConfig(texts={"ask_name": "Hi {name}"})
    assert c.say(broken, "ask_name") == c.ASK_NAME_TEXT          # falls back, never crashes


# --- the nudges' switches ------------------------------------------------------------------
async def _quiet_at_brochure(session, cfg):
    await say(session, cfg, "SFT")
    await say(session, cfg, "Priya")
    await say(session, cfg, button=c.B_BUSINESS, title="Start food business")
    await say(session, cfg, button=c.B_DETAILS, title="View course details")
    return await leads.get(session, NUMBER)


def _templates(rec) -> list[str]:
    return [body for kind, body, _ in rec.sent if kind == "template"]


async def test_nudges_off_sends_nothing_but_the_team_still_gets_the_lead(session, rec, clock):
    cfg = await config.save(session, {"mode": "live", "nudges_on": False}, "t")
    lead = await _quiet_at_brochure(session, cfg)
    rec.clear()
    await nudges.run(session, NOON + timedelta(hours=25))
    assert _templates(rec) == [] and lead.followups_sent == 0
    assert lead.needs_attention and lead.attention_stage == c.BROCHURE_SENT


async def test_a_follow_up_switched_off_is_skipped_and_the_next_comes_on_time(session, rec, clock):
    cfg = await config.save(session, {"mode": "live", "nudges_off": ["followup_2"]}, "t")
    lead = await _quiet_at_brochure(session, cfg)
    rec.clear()
    for hours in (24.1, 72, 144.1):
        await nudges.run(session, NOON + timedelta(hours=hours))
    assert _templates(rec) == ["sft_followup_1", "sft_followup_3"]
    assert lead.followups_sent == 3
    skipped = (await session.execute(select(SftEvent).where(SftEvent.kind == "nudge_skip"))
               ).scalars().all()
    assert len(skipped) == 1 and "Follow-up 2 skipped" in skipped[0].detail


async def test_a_long_quiet_lead_is_not_sent_the_follow_ups_back_to_back(session, cfg, rec, clock):
    lead = await _quiet_at_brochure(session, cfg)
    rec.clear()
    ten_days = NOON + timedelta(days=10)
    for minutes in (0, 5, 10, 15):
        await nudges.run(session, ten_days + timedelta(minutes=minutes))
    assert _templates(rec) == ["sft_followup_1"]
    assert lead.nudged_at == ten_days
    due = nudges.followup_due(lead, cfg)
    assert due - ten_days == timedelta(hours=24)           # a day later, not 5 minutes


async def test_each_lead_shows_its_flow_and_what_comes_next(session, cfg, rec, clock):
    lead = await _quiet_at_brochure(session, cfg)
    f = views.flow_of(lead, cfg)
    assert f.key == "journey" and f.next == "Follow-up 1" and f.at == NOON + timedelta(hours=24)
    await say(session, cfg, button=c.B_ENROL, title="Yes, enroll me")
    f = views.flow_of(lead, cfg)
    assert f.key == "payment" and f.next == "Payment link again"
    lead.with_team = True
    assert views.flow_of(lead, cfg).key == "with_team"
