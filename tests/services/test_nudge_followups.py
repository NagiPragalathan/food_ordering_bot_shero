"""Follow-up journeys and their message library (services/nudges/followup.py,
library.py): the handoff's six-month plan - month stages, reply groups with
their own timing, no repeats, occasions - and importing the messages from
Excel, CSV or JSON.
"""

from __future__ import annotations

import io
import json
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.db.models import (
    ConversationStep,
    Customer,
    InboundMessage,
    MenuItem,
    NudgeJourney,
    NudgeMessage,
    NudgeSend,
    PaymentStatus,
)
from app.integrations.gallabox.sender import use_sender
from app.schemas.inbound import InboundEvent, InboundKind
from app.services import cart
from app.services.conversation.context import FlowContext
from app.services.conversation.engine import _handle_global_intent
from app.services.nudges import config as cfg
from app.services.nudges import followup, library, reports, store
from app.services.nudges.audience import aware
from app.services.nudges.config import NudgeConfigError
from tests.services.test_nudges import Phone, make, now, order, reasons, run, switch_on, wrote_in

# Room for a send every few days without the shop-wide caps getting in the way.
CAPS = {"max_per_customer_per_day": 10, "max_per_customer_per_week": 50, "min_hours_between": 0}
UK = "447700900123"          # not +1: Meta delivers marketing templates there

MESSAGES = [
    {"code": "M1-01", "month": 1, "title": "Rice is sorted", "wa_template": "smo_m1_01",
     "body": "Rice is sorted for today's meal?\n\nSend ORDER FOOD on WhatsApp."},
    {"code": "M1-02", "month": 1, "title": "Same rice, different curry", "wa_template": "smo_m1_02",
     "body": "Same rice. Different curry. Send ORDER FOOD to explore."},
    {"code": "M1-03", "month": 1, "title": "The side dish", "wa_template": "smo_m1_03",
     "body": "Hi {first_name}, the side dish changes the plate. Send ORDER FOOD."},
    {"code": "M2-01", "month": 2, "title": "Kerala, Andhra or Chettinad?", "wa_template": "smo_m2_01",
     "body": "Three regional traditions. Send ORDER FOOD on WhatsApp to explore."},
    {"code": "M2-02", "month": 2, "title": "Regional variety", "wa_template": "smo_m2_02",
     "body": "Bring regional variety to your table. Send ORDER FOOD."},
]


async def add_messages(session, items=MESSAGES):
    for item in items:
        await library.create_message(session, dict(item), actor="test")


async def journey_campaign(session, *, active=True, code=None, **journey):
    return await make(session, "follow_up", active=active, code=code,
                      config={"journey": {"pause_after_chat_hours": 0, **journey}})


async def prospect(session, number=UK, *, days=3, name="Asha Menon") -> Customer:
    customer = Customer(whatsapp_number=number, name=name, created_at=now() - timedelta(days=days))
    session.add(customer)
    await session.flush()
    return customer


async def the_journey(session) -> NudgeJourney:
    return (await session.execute(select(NudgeJourney))).scalar_one()


async def journey_at(session, campaign, customer, *, started_days_ago, bucket="no_response",
                     attempts=1, bucket_attempts=None, recent=()) -> NudgeJourney:
    journey = NudgeJourney(campaign_id=campaign.id, customer_id=customer.id, bucket=bucket,
                           started_at=now() - timedelta(days=started_days_ago),
                           next_due_at=now() - timedelta(minutes=1), attempts=attempts,
                           bucket_attempts=attempts if bucket_attempts is None else bucket_attempts,
                           recent=[{"code": c, "at": now().isoformat()} for c in recent])
    session.add(journey)
    await session.flush()
    return journey


# --- the timing ----------------------------------------------------------------------------------
def test_the_handoffs_cadence_is_the_default():
    journey = cfg.Journey()
    # No response: 3 days x3, 10 days x3, then every 30 (gap after send n).
    assert [journey.no_response.days_until(n) for n in range(1, 9)] == [3, 3, 10, 10, 10, 30, 30, 30]
    # Later: from the reply, every 10 days for 3 sends, then every 30.
    assert [journey.later.days_until(n) for n in range(0, 5)] == [10, 10, 10, 30, 30]
    # No: every 30 days, six at most.
    assert [journey.no.days_until(n) for n in range(0, 7)] == [30] * 6 + [None]
    assert journey.length_days == 183 and journey.stages == 6


async def test_only_the_last_step_may_run_until_the_end(session):
    with pytest.raises(NudgeConfigError, match="only the last step"):
        await make(session, "follow_up", config={"journey": {"later": {"steps": [
            {"every_days": 5}, {"every_days": 10, "times": 2}]}}})


async def test_a_journey_belongs_to_follow_ups_only(session):
    campaign = await journey_campaign(session)
    assert campaign.config["journey"]["pool"] == "SMO"
    assert campaign.config["message"]["button"] == "none"
    await store.update_campaign(session, campaign, {"kind": "never_ordered"}, actor="test")
    assert "journey" not in campaign.config


# --- sending -------------------------------------------------------------------------------------
async def test_month_one_first_then_a_different_one_three_days_later(session):
    await switch_on(session, **CAPS)
    await add_messages(session)
    await journey_campaign(session)
    customer = await prospect(session)
    phone, t0 = Phone(), now()

    await run(session, phone, now=t0)
    assert [m["channel"] for m in phone.sent] == ["template"]
    assert phone.sent[0]["name"] in {"smo_m1_01", "smo_m1_02", "smo_m1_03"}
    journey = await the_journey(session)
    assert (journey.attempts, journey.bucket, journey.status) == (1, "no_response", "active")
    assert aware(journey.next_due_at) == t0 + timedelta(days=3)
    send = (await session.execute(select(NudgeSend))).scalar_one()
    assert send.message_code == journey.last_message_code and send.note.startswith("month 1")
    assert send.dedupe_key == "fu:r1:1"

    report = await run(session, phone, now=t0 + timedelta(days=1))
    assert len(phone.sent) == 1 and reasons(report)[customer.whatsapp_number] == "not_due"

    await run(session, phone, now=t0 + timedelta(days=3, minutes=1))
    assert len(phone.sent) == 2 and phone.sent[1]["name"] != phone.sent[0]["name"]
    assert (await the_journey(session)).attempts == 2


async def test_in_order_then_borrowing_the_nearest_month(session):
    await switch_on(session, **CAPS)
    await add_messages(session)
    await journey_campaign(session, pick="in_order")
    await prospect(session)
    phone, t0 = Phone(), now()
    for day in (0, 3, 6, 16):
        await run(session, phone, now=t0 + timedelta(days=day, minutes=1))
    assert [m["name"] for m in phone.sent] == ["smo_m1_01", "smo_m1_02", "smo_m1_03", "smo_m2_01"]
    borrowed = (await session.execute(select(NudgeSend.note).where(
        NudgeSend.message_code == "M2-01"))).scalar_one()
    assert borrowed == "month 1 all sent recently; from month 2"


async def test_the_second_month_uses_month_two_messages(session):
    await switch_on(session, **CAPS)
    await add_messages(session)
    campaign = await journey_campaign(session)
    customer = await prospect(session, days=40)
    await journey_at(session, campaign, customer, started_days_ago=31, attempts=5)
    phone = Phone()
    await run(session, phone)
    assert phone.sent[0]["name"] in {"smo_m2_01", "smo_m2_02"}


async def test_an_occasion_goes_first_only_between_its_dates(session):
    await switch_on(session, **CAPS)
    await add_messages(session)
    today = now().date()
    await library.create_message(session, {
        "code": "FEST-1", "title": "Festival", "occasion": "Diwali", "wa_template": "smo_fest",
        "starts_on": today - timedelta(days=1), "ends_on": today + timedelta(days=1),
        "body": "Celebrating with family? Send ORDER FOOD."}, actor="test")
    await library.create_message(session, {
        "code": "FEST-OLD", "title": "Last year", "wa_template": "smo_old",
        "starts_on": "2025-11-01", "ends_on": "2025-11-05", "body": "Old festival. ORDER FOOD"},
        actor="test")
    await journey_campaign(session)
    await prospect(session)
    phone = Phone()
    await run(session, phone)
    assert phone.sent[0]["name"] == "smo_fest"
    send = (await session.execute(select(NudgeSend))).scalar_one()
    assert send.note == "occasion: Diwali"


async def test_dishes_off_the_menu_and_coupons_keep_a_message_back(session, menu):
    await switch_on(session, **CAPS)
    await add_messages(session, [
        {"code": "M1-01", "month": 1, "title": "Biryani", "dishes": "Veg Biryani",
         "body": "Biryani today? ORDER FOOD", "wa_template": "a"},
        {"code": "M1-02", "month": 1, "title": "Coupon", "body": "Use {coupon_code}. ORDER FOOD",
         "wa_template": "b"},
        {"code": "M1-03", "month": 1, "title": "Sambar", "dishes": "drumstick sambar",
         "body": "Sambar? ORDER FOOD", "wa_template": "c"}])
    await journey_campaign(session)
    await prospect(session)
    phone = Phone()
    await run(session, phone)
    # Biryani is not on the menu, and the campaign has no coupon.
    assert [m["name"] for m in phone.sent] == ["c"]


async def test_inside_the_window_it_is_the_libraries_text(session):
    await switch_on(session, **CAPS)
    await add_messages(session, [{"code": "M1-03", "month": 1, "title": "Side",
                                  "body": "Hi {first_name}, the side dish. Send ORDER FOOD."}])
    await journey_campaign(session, pause_after_chat_hours=12)
    customer = await prospect(session, number="17325550142")       # US, yet inside the window
    await wrote_in(session, customer, hours_ago=13)
    phone = Phone()
    await run(session, phone)
    assert phone.sent == [{"channel": "text", "to": "17325550142",
                           "body": "Hi Asha, the side dish. Send ORDER FOOD."}]


@pytest.mark.parametrize("number, hours_ago, templated, reason", [
    (UK, 30, False, "no_approved_template"),       # outside the window, no template
    (UK, 2, False, "chatted_recently"),            # 2 h ago, the journey waits 12
])
async def test_why_a_follow_up_is_held_back(session, number, hours_ago, templated, reason):
    await switch_on(session, **CAPS)
    items = [dict(m) for m in MESSAGES]
    if not templated:
        for item in items:
            item.pop("wa_template")
    await add_messages(session, items)
    await journey_campaign(session, pause_after_chat_hours=12)
    customer = await prospect(session, number=number)
    await wrote_in(session, customer, hours_ago=hours_ago)
    report = await run(session, Phone())
    assert reasons(report)[number] == reason


async def test_a_us_number_outside_the_window_gets_the_template(session):
    await switch_on(session, **CAPS)
    await add_messages(session)
    await journey_campaign(session, pause_after_chat_hours=12)
    customer = await prospect(session, number="17325550142")
    await wrote_in(session, customer, hours_ago=30)
    phone = Phone()
    await run(session, phone)
    assert phone.sent[0]["channel"] == "template" and phone.sent[0]["to"] == "17325550142"


async def test_nobody_mid_order_and_nothing_without_messages(session):
    await switch_on(session, **CAPS)
    await journey_campaign(session)
    customer = await prospect(session)
    assert reasons(await run(session, Phone()))[UK] == "no_message_available"

    await add_messages(session)
    conversation, _ = await cart.for_customer(session, customer)
    conversation.step = ConversationStep.CART_REVIEW
    conversation.last_message_at = now() - timedelta(hours=1)
    await session.flush()
    assert reasons(await run(session, Phone()))[UK] == "ordering_now"


async def test_a_dry_run_leaves_the_journey_alone(session):
    await switch_on(session, dry_run=True, **CAPS)
    await add_messages(session)
    await journey_campaign(session)
    await prospect(session)
    await run(session, Phone())
    assert (await session.execute(select(NudgeJourney))).first() is None
    assert (await session.execute(select(NudgeSend.status))).scalar_one() == "dry_run"


async def test_three_failed_sends_count_as_the_attempt(session):
    await switch_on(session, **CAPS)
    await add_messages(session)
    await journey_campaign(session)
    await prospect(session)
    for _ in range(3):
        await run(session, Phone(fail=True))
    journey = await the_journey(session)
    assert (journey.attempts, journey.failures) == (1, 0)
    assert aware(journey.next_due_at) > now() + timedelta(days=2)


# --- how it ends ---------------------------------------------------------------------------------
async def test_an_order_ends_the_journey(session):
    await switch_on(session, **CAPS)
    await add_messages(session)
    await journey_campaign(session)
    customer = await prospect(session)
    await run(session, Phone())
    await order(session, customer, days_ago=0)
    phone = Phone()
    await run(session, phone, now=now() + timedelta(days=4))
    journey = await the_journey(session)
    assert (journey.status, journey.end_reason) == ("converted", "ordered") and not phone.sent


async def test_the_no_group_stops_after_six(session):
    await switch_on(session, **CAPS)
    await add_messages(session)
    campaign = await journey_campaign(session)
    customer = await prospect(session, days=200)
    await journey_at(session, campaign, customer, started_days_ago=20, bucket="no",
                     attempts=8, bucket_attempts=5)
    await run(session, Phone())
    journey = await the_journey(session)
    assert (journey.status, journey.end_reason, journey.bucket_attempts) == (
        "ended", "attempt_limit", 6)


async def test_six_months_after_the_first_send_it_is_over(session):
    await switch_on(session, **CAPS)
    await add_messages(session)
    campaign = await journey_campaign(session)
    customer = await prospect(session, days=300)
    await journey_at(session, campaign, customer, started_days_ago=184, attempts=11)
    report = await run(session, Phone())
    assert reasons(report)[UK] == "journey_finished"
    assert (await the_journey(session)).end_reason == "length_reached"


# --- replies ---------------------------------------------------------------------------------------
async def reply(session, customer, text, *, step=ConversationStep.START, hours_ago=48):
    conversation, _ = await cart.for_customer(session, customer)
    conversation.step = step
    conversation.last_message_at = now() - timedelta(hours=hours_ago)
    ctx = FlowContext(session=session, customer=customer, conversation=conversation,
                      event=InboundEvent(whatsapp_number=customer.whatsapp_number,
                                         kind=InboundKind.TEXT, text=text))
    phone = Phone()
    with use_sender(phone):
        handled = await _handle_global_intent(ctx)
    return handled, phone


async def test_later_moves_them_and_the_wait_starts_again(session):
    await switch_on(session, **CAPS)
    await add_messages(session)
    await journey_campaign(session)
    customer = await prospect(session)
    await run(session, Phone())

    handled, phone = await reply(session, customer, "Later!")
    journey = await the_journey(session)
    assert handled and (journey.bucket, journey.bucket_attempts, journey.last_reply) == (
        "later", 0, "later")
    assert aware(journey.next_due_at) > now() + timedelta(days=9.9)
    assert "check back" in phone.sent[0]["body"]


async def test_no_mid_order_answers_the_bot_not_the_journey(session):
    await switch_on(session, **CAPS)
    await add_messages(session)
    await journey_campaign(session)
    customer = await prospect(session)
    await run(session, Phone())
    handled, _ = await reply(session, customer, "no", step=ConversationStep.AWAIT_APARTMENT,
                             hours_ago=0.1)
    assert not handled and (await the_journey(session)).bucket == "no_response"


async def test_no_can_be_a_stop(session):
    await switch_on(session, no_means_opt_out=True, **CAPS)
    await add_messages(session)
    await journey_campaign(session)
    customer = await prospect(session)
    await run(session, Phone())
    handled, _ = await reply(session, customer, "no thanks")
    journey = await the_journey(session)
    assert handled and customer.nudges_opted_out_at is not None
    assert (journey.status, journey.end_reason) == ("ended", "declined")


async def test_later_from_someone_never_followed_up_is_just_a_message(session, customer):
    handled, _ = await reply(session, customer, "later")
    assert not handled


async def test_keyword_mode_still_hears_a_reply_to_a_follow_up(session, monkeypatch):
    from app.core.config import settings
    from app.services import reply_triggers

    monkeypatch.setattr(settings, "bot_reply_trigger", "keywords")
    monkeypatch.setattr(settings, "bot_trigger_keywords",
                        json.dumps([{"keyword": "order food", "match": "contains"}]))
    await switch_on(session, **CAPS)
    await add_messages(session)
    await journey_campaign(session)
    customer = await prospect(session)
    await run(session, Phone())
    stranger = await prospect(session, number="447700900999")       # never followed up
    event = InboundEvent(whatsapp_number=customer.whatsapp_number, kind=InboundKind.TEXT,
                         text="not now")
    assert (await reply_triggers.decide(session, event)).answer
    other = InboundEvent(whatsapp_number=stranger.whatsapp_number, kind=InboundKind.TEXT,
                         text="not now")
    assert not (await reply_triggers.decide(session, other)).answer


# --- the team's controls ---------------------------------------------------------------------------
async def test_the_team_can_move_pause_restart_and_end(session):
    await switch_on(session, **CAPS)
    await add_messages(session)
    await journey_campaign(session)
    await prospect(session)
    await run(session, Phone())
    journey = await the_journey(session)

    await followup.act(session, journey, "move", bucket="no", actor="ops")
    assert journey.bucket == "no" and aware(journey.next_due_at) > now() + timedelta(days=29)
    await followup.act(session, journey, "pause", actor="ops")
    assert reasons(await run(session, Phone()))[UK] == "journey_paused"
    await followup.act(session, journey, "resume", actor="ops")
    await followup.act(session, journey, "due_now", actor="ops")
    phone = Phone()
    await run(session, phone)
    assert len(phone.sent) == 1
    await followup.act(session, journey, "end", actor="ops")
    assert journey.end_reason == "ended_by_team"
    await followup.act(session, journey, "restart", actor="ops")
    assert (journey.round, journey.attempts, journey.status) == (2, 0, "active")
    phone = Phone()
    await run(session, phone)
    assert len(phone.sent) == 1 and (await the_journey(session)).attempts == 1
    with pytest.raises(NudgeConfigError):
        await followup.act(session, journey, "move", bucket="maybe", actor="ops")


async def test_the_summary_counts_by_group(session):
    await switch_on(session, **CAPS)
    await add_messages(session)
    campaign = await journey_campaign(session)
    await prospect(session)
    await run(session, Phone())
    summary = (await followup.summary(session))[str(campaign.id)]
    assert summary["total"] == 1 and summary["by_bucket"] == {"no_response": 1}
    assert summary["by_stage"] == {1: 1} and summary["due_now"] == 0


async def test_each_message_has_its_results(session):
    await switch_on(session, **CAPS)
    await add_messages(session, MESSAGES[:1])
    await journey_campaign(session)
    customer = await prospect(session)
    await run(session, Phone())
    session.add(InboundMessage(provider_message_id="reply-1", whatsapp_number=UK, payload={},
                               created_at=now() + timedelta(minutes=5)))
    await session.flush()
    await order(session, customer, days_ago=0)
    stats = await reports.stats(session, days=30)
    assert stats["messages"] == [{"code": "M1-01", "sent": 1, "failed": 0, "customers": 1,
                                  "replies": 1, "keywords": 0, "delivered": 0, "read": 0,
                                  "conversions": 1, "revenue": "30.00", "reply_rate": 1.0,
                                  "keyword_rate": 0.0, "average_order": "30.00"}]


# --- the library and its files ----------------------------------------------------------------------
async def test_a_message_is_checked_when_saved(session):
    message = await library.create_message(session, {
        "code": " m1-01 ", "title": "One", "body": "Hi {first_name}", "buckets": "Later, no",
        "month": "Month 2", "wa_params": "{first_name} | {shop_name}"}, actor="test")
    assert (message.code, message.month, message.buckets, message.wa_params) == (
        "M1-01", 2, ["later", "no"], ["{first_name}", "{shop_name}"])
    for bad, why in (({"code": "M1-01"}, "already a message"),
                     ({"code": "X", "body": "{cart_items}"}, "not available"),
                     ({"code": "X", "body": "{nope}"}, "not a placeholder"),
                     ({"code": "X", "buckets": "maybe"}, "Groups"),
                     ({"code": "X", "starts_on": "2026-11-05", "ends_on": "2026-11-01"}, "before")):
        with pytest.raises(NudgeConfigError, match=why):
            await library.create_message(session, {"title": "T", "body": "B", **bad}, actor="test")
    copy = await library.duplicate_message(session, message, actor="test")
    assert copy.code == "M1-01-COPY" and copy.is_active is False


async def test_an_excel_export_imports_straight_back(session):
    await add_messages(session)
    messages = await library.list_messages(session)
    sheet = library.to_xlsx(messages)
    result = await library.import_rows(session, library.read_file("m.xlsx", sheet), actor="t")
    assert result["counts"] == {"create": 0, "update": 0, "unchanged": 5, "skip": 0, "error": 0}

    from openpyxl import load_workbook
    book = load_workbook(io.BytesIO(sheet))
    rows = book["Messages"]
    assert [c.value for c in rows[1]][:6] == ["ID", "Pool", "Month", "Title", "Angle", "Message"]
    rows["D2"] = "A new title"
    rows.append(["M3-01", "SMO", 3, "Third month", "", "Month three. Send ORDER FOOD"])
    rows.append(["M3-02", "SMO", 3, "", "", "No title"])
    out = io.BytesIO()
    book.save(out)
    parsed = library.read_file("m.xlsx", out.getvalue())

    check = await library.import_rows(session, parsed, actor="t")
    assert check["counts"] == {"create": 1, "update": 1, "unchanged": 4, "skip": 0, "error": 1}
    assert (await library.by_code(session, "M3-01")) is None          # only checked

    done = await library.import_rows(session, parsed, commit=True, actor="t")
    assert done["committed"] and (await library.by_code(session, "M1-01")).title == "A new title"
    assert (await library.by_code(session, "M3-01")).month == 3
    errors = [r for r in done["rows"] if r["action"] == "error"]
    assert errors[0]["code"] == "M3-02" and "Title is required" in errors[0]["error"]


async def test_csv_with_loose_headings_and_add_only(session):
    await add_messages(session, MESSAGES[:1])
    text = ("Template ID;Stage;Name;Body;Active\n"
            "M1-01;1;Changed;Changed text ORDER FOOD;yes\n"
            "M1-09;1;Ninth;Ninth text ORDER FOOD;no\n")
    parsed = library.read_file("plan.csv", text.encode("utf-8"))
    result = await library.import_rows(session, parsed, mode="add_only", commit=True, actor="t")
    assert result["counts"]["create"] == 1 and result["counts"]["skip"] == 1
    assert (await library.by_code(session, "M1-01")).title == "Rice is sorted"
    assert (await library.by_code(session, "M1-09")).is_active is False


async def test_json_including_the_starter_kits_layout(session):
    await add_messages(session, MESSAGES[:2])
    kit = json.dumps([{"id": "M1-01", "month": 1, "title": "Rice is sorted", "body": ""},
                      {"id": "M6-09", "month": 6, "title": "Respectful close",
                       "body": "We'll be here for your next meal. Send ORDER FOOD."}])
    result = await library.import_rows(session, library.read_file("k.json", kit.encode()),
                                       turn_off_missing=True, commit=True, actor="t")
    assert result["counts"]["create"] == 1 and result["counts"]["error"] == 1
    assert "Message is empty" in result["rows"][0]["error"]
    assert result["turned_off"] == ["M1-02"]
    assert (await library.by_code(session, "M1-02")).is_active is False

    exported = json.loads(library.to_json(await library.list_messages(session)))
    again = await library.import_rows(session, library.read_file(
        "e.json", json.dumps(exported).encode()), actor="t")
    assert again["counts"]["unchanged"] == 3


def test_files_that_cannot_be_read_say_why():
    for name, raw, why in (("a.json", b"{oops", "not valid JSON"),
                           ("a.csv", b"Title,Message\nx,y\n", "ID column"),
                           ("a.xls", b"\xd0\xcf\x11\xe0", "xlsx"),
                           ("a.csv", b"   ", "empty")):
        with pytest.raises(NudgeConfigError, match=why):
            library.read_file(name, raw)


def test_the_sample_files_import_cleanly():
    for fmt in ("xlsx", "csv", "json"):
        content, _ = library.sample(fmt)
        parsed = library.read_file(f"s.{fmt}", content)
        assert [code for _, row in parsed.rows for code in [row["code"]]] == [
            "M1-01", "M2-01", "FEST-DIWALI"]
        assert library.clean(parsed.rows[2][1])["starts_on"] == date(2026, 11, 1)


async def test_messages_are_listed_in_journey_order(session):
    await add_messages(session, [
        {"code": "M1-10", "month": 1, "title": "t", "body": "b"},
        {"code": "M1-2", "month": 1, "title": "t", "body": "b"},
        {"code": "ANY", "title": "t", "body": "b"},
        {"code": "M2-01", "month": 2, "title": "t", "body": "b"}])
    assert [m.code for m in await library.list_messages(session)] == ["M1-2", "M1-10", "M2-01", "ANY"]
    assert (await session.execute(select(NudgeMessage.pool).distinct())).scalars().all() == ["SMO"]


async def test_a_menu_item_name_matches_loosely(session, menu):
    names = await followup.menu_dishes(session, {})
    assert "drumstick sambar" in names
    item = (await session.execute(select(MenuItem).limit(1))).scalar_one()
    assert item.retailer_id in names
    assert PaymentStatus.PAID  # the enum the converted check uses
