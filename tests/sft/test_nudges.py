"""SFT's follow-ups, reminders and team tasks (app/sft/nudges.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.sft import config, leads, nudges, payments
from app.sft import content as c
from tests.sft.conftest import NUMBER, say

# 15:00 UTC is 10:00 in New York: inside the default 9-20 sending hours.
NOON = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    """Everything happens at NOON, and the run looks `hours` later."""
    monkeypatch.setattr(leads, "now", lambda: NOON)
    for module in ("app.sft.flow", "app.sft.nudges", "app.sft.sending", "app.sft.payments"):
        monkeypatch.setattr(f"{module}.now", lambda: NOON, raising=False)


async def _at_brochure(session, cfg):
    await say(session, cfg, "SFT")
    await say(session, cfg, "Priya")
    await say(session, cfg, button=c.B_BUSINESS, title="Start food business")
    await say(session, cfg, button=c.B_DETAILS, title="View course details")
    return await leads.get(session, NUMBER)


async def _run(session, hours: float) -> int:
    return await nudges.run(session, NOON + timedelta(hours=hours))


def _templates(rec) -> list[str]:
    return [body for kind, body, _ in rec.sent if kind == "template"]


async def test_follow_ups_go_after_24_60_and_144_quiet_hours(session, cfg, rec, clock):
    lead = await _at_brochure(session, cfg)
    rec.clear()
    assert await _run(session, 23) == 0
    assert _templates(rec) == []
    await _run(session, 24.1)
    assert _templates(rec) == ["sft_followup_1"]
    await _run(session, 30)
    assert _templates(rec) == ["sft_followup_1"]       # not again
    await _run(session, 60.1)                          # 22:06 in New York: waits
    assert _templates(rec) == ["sft_followup_1"]
    await _run(session, 72)                            # 10:00 the next morning
    await _run(session, 144.1)
    assert _templates(rec) == ["sft_followup_1", "sft_followup_2", "sft_followup_3"]
    assert lead.followups_sent == 3
    await _run(session, 400)
    assert len(_templates(rec)) == 3


async def test_after_the_last_follow_up_the_team_gets_the_lead(session, cfg, rec, clock,
                                                             monkeypatch):
    lead = await _at_brochure(session, cfg)
    for hours in (24.1, 72, 144.1):
        await _run(session, hours)
    await leads.resolve(session, lead, "called", by="Ops")      # the brochure task, closed
    # A day after the last follow-up (its event is stamped NOON in this test).
    await _run(session, 200)
    assert lead.needs_attention and lead.attention_stage == "followups_done"


async def test_the_brochure_drop_off_becomes_a_team_task_once(session, cfg, rec, clock):
    lead = await _at_brochure(session, cfg)
    await _run(session, 23)
    assert not lead.needs_attention
    await _run(session, 24.5)
    assert lead.needs_attention and lead.attention_stage == c.BROCHURE_SENT
    await leads.resolve(session, lead, "called", by="Ops")
    await _run(session, 48)
    assert not lead.needs_attention                     # once per stage


async def test_a_new_lead_who_never_answers_goes_to_the_team_after_2_hours(session, cfg, rec,
                                                                         clock):
    await say(session, cfg, "SFT")
    lead = await leads.get(session, NUMBER)
    await _run(session, 2.1)
    assert lead.needs_attention and lead.attention_stage == c.NEW


async def test_nothing_goes_outside_the_sending_hours(session, cfg, rec, clock):
    await _at_brochure(session, cfg)
    rec.clear()
    # 24h + 11h later is 21:00 in New York.
    await _run(session, 35)
    assert _templates(rec) == []


async def test_nothing_goes_to_a_lead_with_the_team(session, cfg, rec, clock):
    lead = await _at_brochure(session, cfg)
    await say(session, cfg, button=c.B_ASK, title="Ask a question")
    await say(session, cfg, "Is it in English?")
    rec.clear()
    for hours in (25, 61, 145, 300):
        await _run(session, hours)
    assert rec.sent == [] and lead.with_team


async def test_nothing_goes_to_someone_who_said_no(session, cfg, rec, clock):
    await _at_brochure(session, cfg)
    await say(session, cfg, "Not interested")
    rec.clear()
    await _run(session, 200)
    assert rec.sent == []


async def test_a_handed_back_lead_is_followed_up_again(session, cfg, rec, clock):
    lead = await _at_brochure(session, cfg)
    await say(session, cfg, button=c.B_ASK, title="Ask a question")
    await say(session, cfg, "Is it in English?")
    lead.with_team = False                                # the team answered
    lead.step = c.IDLE
    rec.clear()
    await _run(session, 24.5)
    assert _templates(rec) == ["sft_followup_1"]


async def test_the_payment_link_is_sent_again_after_an_hour(session, cfg, rec, clock):
    lead = await _at_brochure(session, cfg)
    await say(session, cfg, "yes")
    rec.clear()
    await _run(session, 0.5)
    assert rec.sent == []
    await _run(session, 1.1)
    kind, body, (url, _) = rec.last()
    assert kind == "link" and "isn't complete yet" in body and lead.pay_token in url
    rec.clear()
    await _run(session, 2)
    assert rec.sent == []                                 # once


async def test_unpaid_follow_up_one_is_the_payment_reminder(session, cfg, rec, clock):
    lead = await _at_brochure(session, cfg)
    await say(session, cfg, "yes")
    lead.payment_reminded_at = NOON
    rec.clear()
    await _run(session, 24.5)
    assert _templates(rec) == ["sft_payment_reminder"]
    assert lead.needs_attention and lead.attention_priority == "high"   # 12h, high priority


async def test_a_day_after_paying_they_are_asked_about_the_course(session, rec, clock):
    cfg = await config.save(session, {"mode": "live", "course_url": "https://x.example/c"}, "t")
    lead = await _at_brochure(session, cfg)
    await payments.mark_paid(session, lead, cfg, amount=Decimal("99"), currency="usd")
    rec.clear()
    await _run(session, 23)
    assert rec.sent == []
    await _run(session, 24.5)
    assert _templates(rec) == ["sft_course_check"] and lead.step == c.ACCESS
    await say(session, cfg, "Yes, I'm in")
    assert lead.access_confirmed_at is not None


async def test_a_paid_lead_who_never_confirms_goes_to_the_team(session, rec, clock):
    cfg = await config.save(session, {"mode": "live", "course_url": "https://x.example/c"}, "t")
    lead = await _at_brochure(session, cfg)
    await payments.mark_paid(session, lead, cfg, amount=Decimal("99"), currency="usd")
    await _run(session, 24.5)
    await _run(session, 49)
    assert lead.needs_attention and lead.attention_stage == "access"


async def test_maybe_later_checks_in_on_the_day_chosen(session, cfg, rec, clock):
    lead = await _at_brochure(session, cfg)
    await say(session, cfg, "Maybe later")
    await say(session, cfg, button=c.B_WEEK, title="In a week")
    rec.clear()
    await _run(session, 24 * 6)
    assert _templates(rec) == []
    await _run(session, 24 * 7 + 1)
    assert _templates(rec) == ["sft_nurture"]
    await _run(session, 24 * 37 + 1)
    assert _templates(rec) == ["sft_nurture", "sft_nurture"]
    await _run(session, 24 * 200)
    assert len(_templates(rec)) == 2


async def test_an_unapproved_template_outside_the_window_is_skipped(session, cfg, rec, clock,
                                                                  monkeypatch):
    from app.integrations.gallabox import template_status

    async def no(name):
        return False

    monkeypatch.setattr(template_status, "is_approved", no)
    lead = await _at_brochure(session, cfg)
    rec.clear()
    await _run(session, 24.5)
    assert rec.sent == [] and lead.followups_sent == 1      # counted, not sent


async def test_an_unapproved_template_inside_the_window_goes_as_buttons(session, cfg, rec,
                                                                      clock, monkeypatch):
    from app.integrations.gallabox import template_status

    async def no(name):
        return False

    monkeypatch.setattr(template_status, "is_approved", no)
    lead = await _at_brochure(session, cfg)
    lead.last_inbound_at = NOON + timedelta(hours=20)      # they wrote recently
    rec.clear()
    await _run(session, 24.5)
    kind, body, buttons = rec.last()
    assert kind == "buttons" and body.startswith("Hi Priya, just checking in")
    assert [b for _, b in buttons] == ["Enroll now", "Ask a question", "Not interested"]
    assert buttons[0][0] == c.B_ENROL


async def test_off_sends_nothing(session, rec, clock):
    cfg = await config.save(session, {"mode": "live"}, "t")
    await _at_brochure(session, cfg)
    await config.save(session, {"mode": "off"}, "t")
    rec.clear()
    assert await _run(session, 200) == 0 and rec.sent == []
