"""The SMO developer handoff (docs/Shero_SMO_Only_Developer_Handoff.pdf), its
acceptance checklist and test scenarios (sections 12 and 13), end to end:
cadence switches, ORDER FOOD and an abandoned flow, holds, occasion rules,
angles, held-back logging, time zones, the Zoho retargeting fields, delivery
receipts and the dashboard.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.core.exceptions import IntegrationError
from app.db.models import (FlowStatus, JourneyStatus, MenuItem, NudgeJourney, NudgeSend, NudgeSkip,
                           PaymentStatus)
from app.integrations.zoho import fields as f
from app.services.nudges import config as cfg
from app.services.nudges import crm_fields, delivery, followup, library, optout, reports
from app.services.nudges.audience import aware
from tests.services.test_nudge_followups import (CAPS, MESSAGES, UK, add_messages, journey_at,
                                                 journey_campaign, prospect, the_journey)
from tests.services.test_nudges import Phone, now, order, reasons, run, switch_on


class Gallabox(Phone):
    """A phone whose sends come back with Gallabox's message id."""

    def __init__(self):
        super().__init__()
        self.count = 0

    async def send_template_only(self, to, spec, *values, button_value=None):
        await super().send_template_only(to, spec, *values, button_value=button_value)
        self.count += 1
        return {"id": f"wamid.{self.count}", "status": "accepted"}


async def ready(session, **journey):
    await switch_on(session, **CAPS)
    await add_messages(session)
    campaign = await journey_campaign(session, **journey)
    return campaign, await prospect(session)


# --- section 13: the cadence switches --------------------------------------------------------------
def test_the_cadence_switches_where_the_handoff_says():
    journey = cfg.Journey()
    j = NudgeJourney(bucket="no_response", attempts=0, bucket_attempts=0, status="active",
                     started_at=now(), next_due_at=now())
    gaps = []
    for _ in range(7):
        j.bucket_attempts += 1
        before = now()
        followup.schedule_next(j, journey, before)
        gaps.append(round((aware(j.next_due_at) - before).total_seconds() / 86400))
    # After attempt 3 the interval is 10 days; after three 10-day ones, 30.
    assert gaps == [3, 3, 10, 10, 10, 30, 30]

    later = NudgeJourney(bucket="later", bucket_attempts=3, status="active", started_at=now(),
                         next_due_at=now())
    followup.schedule_next(later, journey, now())
    assert round((aware(later.next_due_at) - now()).total_seconds() / 86400) == 30

    no = NudgeJourney(bucket="no", bucket_attempts=6, status="active", started_at=now(),
                      next_due_at=now())
    followup.schedule_next(no, journey, now())
    assert (no.status, no.end_reason, followup.crm_bucket(no)) == ("ended", "attempt_limit",
                                                                    "Dead Lead")


async def test_six_months_marks_a_dead_lead_and_keeps_the_customer(session):
    campaign, customer = await ready(session)
    await journey_at(session, campaign, customer, started_days_ago=184, attempts=12)
    report = await run(session, Phone())
    assert reasons(report)[UK] == "journey_finished"
    journey = await the_journey(session)
    assert followup.crm_bucket(journey) == "Dead Lead"
    assert followup.crm_status(journey, customer) == "Completed"
    assert crm_fields.values(customer, journey, None)[f.R_DEAD_REASON] == "Ran its full length"


# --- ORDER FOOD and the ordering flow ----------------------------------------------------------
async def test_order_food_pauses_follow_ups_and_an_abandoned_flow_resumes_them(session):
    campaign, customer = await ready(session, resume_after_days=3)
    phone = Gallabox()
    await run(session, phone)
    journey = await the_journey(session)
    journey.next_due_at = now() - timedelta(minutes=1)          # the next one is due now

    assert await followup.flow_started(session, customer, "ORDER FOOD") == 1
    assert (journey.flow_status, journey.last_keyword) == ("active", "ORDER FOOD")
    assert followup.crm_bucket(journey) == "Engaged"
    assert followup.crm_status(journey, customer) == "Paused"
    send = (await session.execute(select(NudgeSend))).scalar_one()
    assert send.keyword_at is not None                         # credited to the follow-up

    report = await run(session, phone)                         # due, but they are ordering
    assert reasons(report)[UK] == "in_ordering_flow" and len(phone.sent) == 1

    # No order three days on: the flow is abandoned and the follow-ups resume.
    journey.flow_started_at = now() - timedelta(days=3, minutes=1)
    await run(session, phone)
    assert journey.flow_status == "abandoned" and len(phone.sent) == 2
    assert journey.bucket == "no_response"


async def test_an_order_after_order_food_completes_the_flow(session):
    campaign, customer = await ready(session)
    await run(session, Phone())
    await followup.flow_started(session, customer, "ORDER FOOD")
    await order(session, customer, days_ago=0)
    await run(session, Phone())
    journey = await the_journey(session)
    assert (journey.status, journey.flow_status) == ("converted", "completed")
    assert followup.crm_bucket(journey) == "Engaged"


async def test_the_trigger_keyword_in_a_chat_starts_the_flow(session, monkeypatch):
    from app.core.config import settings
    from app.schemas.inbound import InboundEvent, InboundKind
    from app.services import cart
    from app.services.conversation.context import FlowContext
    from app.services.conversation.engine import _note_keyword

    monkeypatch.setattr(settings, "bot_trigger_keywords", '[{"keyword": "ORDER FOOD"}]')
    campaign, customer = await ready(session)
    await run(session, Phone())
    conversation, _ = await cart.for_customer(session, customer)
    for text, expected in (("Hello", "not_started"), ("Order Food", "active")):
        ctx = FlowContext(session=session, customer=customer, conversation=conversation,
                          event=InboundEvent(whatsapp_number=customer.whatsapp_number,
                                             kind=InboundKind.TEXT, text=text))
        await _note_keyword(ctx)
        assert (await the_journey(session)).flow_status == expected


# --- holds and opt-outs ----------------------------------------------------------------------------
async def test_a_complaint_hold_blocks_every_nudge_until_lifted(session):
    campaign, customer = await ready(session)
    await optout.hold(session, customer, "complaint", "Late delivery, refund pending", actor="ops")
    report = await run(session, Phone())
    assert reasons(report)[UK] == "on_hold"
    assert crm_fields.values(customer, None, None)[f.R_SUPPRESSION] == "Complaint"
    assert followup.crm_status(None, customer) == "Suppressed"
    with pytest.raises(cfg.NudgeConfigError):
        await optout.hold(session, customer, "angry", "", actor="ops")

    assert await optout.release(session, customer, actor="ops")
    phone = Phone()
    await run(session, phone)
    assert len(phone.sent) == 1 and customer.nudge_hold is None


async def test_an_opt_out_after_the_send_was_scheduled_blocks_it(session):
    campaign, customer = await ready(session)
    await run(session, Phone())
    (await the_journey(session)).next_due_at = now() - timedelta(minutes=1)
    await optout.opt_out(session, customer, source="whatsapp")
    report = await run(session, Phone())
    assert reasons(report)[UK] == "opted_out"
    assert crm_fields.values(customer, await the_journey(session), None)[f.R_OPT_IN] is False


# --- occasions ---------------------------------------------------------------------------------------
OCCASION = {"code": "FEST-DIWALI", "title": "Diwali sides", "occasion": "Diwali",
            "body": "Celebrating Diwali? Send ORDER FOOD.", "wa_template": "smo_fest_diwali"}


async def test_an_occasion_past_its_cutoff_is_skipped_and_logged(session):
    campaign, customer = await ready(session)
    today = now().date()
    await library.create_message(session, {**OCCASION, "starts_on": today - timedelta(days=5),
                                           "ends_on": today + timedelta(days=5),
                                           "order_by": today - timedelta(days=1)}, actor="test")
    phone = Phone()
    await run(session, phone)
    send = (await session.execute(select(NudgeSend))).scalar_one()
    assert send.message_code.startswith("M1-")                 # a normal message instead
    assert "passed over FEST-DIWALI (its ordering cutoff has passed)" in send.note
    skip = (await session.execute(select(NudgeSkip))).scalar_one()
    assert (skip.message_code, skip.reason) == ("FEST-DIWALI", "occasion_cutoff_passed")


async def test_an_occasion_needs_its_area_and_a_dish_on_the_menu(session, outlet, far_outlet, menu):
    campaign, customer = await ready(session)
    customer.preferred_outlet_id = outlet.id
    today = now().date()
    window = {"starts_on": today - timedelta(days=1), "ends_on": today + timedelta(days=5)}
    await library.create_message(session, {**OCCASION, **window, "kitchens": far_outlet.code},
                                 actor="test")
    await library.create_message(session, {**OCCASION, **window, "code": "FEST-PONGAL",
                                           "dishes": "Sakkarai Pongal"}, actor="test")
    await run(session, Phone())
    skips = {(s.message_code, s.reason) for s in (await session.execute(select(NudgeSkip))).scalars()}
    assert skips == {("FEST-DIWALI", "occasion_not_in_area"),
                     ("FEST-PONGAL", "occasion_dish_unavailable")}

    # In the area, with the dish on the menu: the occasion goes first.
    (await the_journey(session)).next_due_at = now() - timedelta(minutes=1)
    message = (await session.execute(select(library.NudgeMessage).where(
        library.NudgeMessage.code == "FEST-DIWALI"))).scalar_one()
    message.kitchens = [outlet.code]
    phone = Phone()
    await run(session, phone)
    assert phone.sent[0]["name"] == "smo_fest_diwali"


# --- angles -------------------------------------------------------------------------------------------
async def test_the_same_angle_is_not_sent_twice_in_a_row(session):
    await switch_on(session, **CAPS)
    await add_messages(session, [
        {**MESSAGES[0], "angle": "Meal completion"},
        {**MESSAGES[1], "angle": "Meal completion!"},           # near-identical: same angle
        {**MESSAGES[2], "angle": "Craving"}])
    campaign = await journey_campaign(session, avoid_repeat=1, avoid_angle_repeat=1)
    customer = await prospect(session)
    await journey_at(session, campaign, customer, started_days_ago=1, recent=["M1-01"])
    phone = Phone()
    await run(session, phone)
    assert phone.sent[0]["name"] == "smo_m1_03"               # M1-02 shares M1-01's angle
    assert cfg.Journey().avoid_angle_repeat == 2


# --- held back, time zones ------------------------------------------------------------------------------
async def test_a_due_follow_up_held_back_is_logged_with_its_reason(session):
    campaign, customer = await ready(session)
    await run(session, Phone())
    journey = await the_journey(session)
    report = await run(session, Phone())                       # not due: not a hold-back
    assert reasons(report)[UK] == "not_due"
    assert (await session.execute(select(NudgeSkip))).first() is None

    journey.next_due_at = now() - timedelta(minutes=1)
    await switch_on(session, **CAPS, send_window={"start": "00:00", "end": "00:01"})
    for _ in range(2):
        await run(session, Phone())
    skip = (await session.execute(select(NudgeSkip))).scalar_one()
    assert (skip.reason, skip.count, skip.message_code) == ("quiet_hours", 2, "")
    assert journey.last_skip_reason == "quiet_hours"
    dashboard = await reports.smo_dashboard(session)
    assert dashboard["deferred"] == {"quiet_hours": 2}


async def test_the_time_zone_and_where_it_came_from_are_recorded(session, outlet):
    campaign, customer = await ready(session)
    await run(session, Phone())
    journey = await the_journey(session)
    assert (journey.timezone, journey.timezone_source) == ("America/New_York", "fallback")

    other = await prospect(session, "447700900999")
    other.preferred_outlet_id = outlet.id
    outlet.timezone = "America/Chicago"
    await run(session, Phone())
    theirs = (await session.execute(select(NudgeJourney).where(
        NudgeJourney.customer_id == other.id))).scalar_one()
    assert (theirs.timezone, theirs.timezone_source) == ("America/Chicago", "kitchen")


# --- Zoho retargeting fields --------------------------------------------------------------------------
async def test_the_zoho_fields_follow_the_journey(session, monkeypatch):
    campaign, customer = await ready(session)
    customer.zoho_lead_id = "lead-1"
    writes = []

    async def update_record(module, record_id, payload):
        if f.R_TIMEZONE in payload:                            # a field this CRM lacks
            raise IntegrationError("zoho", "invalid", payload={
                "code": "INVALID_DATA", "details": {"api_name": f.R_TIMEZONE}})
        writes.append((module, record_id, dict(payload)))

    async def no_note(*a, **kw):
        return None

    monkeypatch.setattr(crm_fields.crm, "update_record", update_record)
    monkeypatch.setattr(followup.engagement, "add_note", no_note)
    monkeypatch.setattr("app.services.nudges.runner.engagement.add_note", no_note)
    await run(session, Phone())
    module, record_id, payload = writes[-1]
    assert (module, record_id) == (f.LEADS, "lead-1")
    assert payload[f.R_BUCKET] == "No-response" and payload[f.R_STATUS] == "Active"
    assert payload[f.R_ATTEMPTS] == 1 and payload[f.R_ACTIVE_JOURNEY] == "SMO"
    assert payload[f.R_ENTRY_VERTICAL] == "SMO" and payload[f.R_CONTACT_KEY] == UK
    assert payload[f.R_LAST_TEMPLATE].startswith("M1-") and payload[f.R_FLOW_STATUS] == "Not started"
    assert f.R_TIMEZONE not in payload
    journey = await the_journey(session)
    assert journey.zoho_synced_at and "Customer_Timezone" in journey.zoho_error

    await followup.flow_started(session, customer, "ORDER FOOD")
    assert writes[-1][2][f.R_BUCKET] == "Engaged" and writes[-1][2][f.R_LAST_KEYWORD] == "ORDER FOOD"


def test_every_retargeting_field_is_in_the_zoho_setup():
    from app.integrations.zoho import schema

    names = {spec.api_name for spec in schema.FIELDS if spec.module == f.LEADS}
    assert {f.R_BUCKET, f.R_STATUS, f.R_START, f.R_ATTEMPTS, f.R_BUCKET_ATTEMPTS, f.R_LAST_DATE,
            f.R_NEXT_DATE, f.R_ACTIVE_JOURNEY, f.R_LAST_TEMPLATE, f.R_TEMPLATE_HISTORY,
            f.R_TIMEZONE, f.R_LAST_KEYWORD, f.R_FLOW_STATUS, f.R_LAST_ORDER_DATE,
            f.R_LAST_ORDER_STATUS, f.R_DEAD_REASON, f.R_SUPPRESSION, f.R_OPT_IN,
            f.R_CONTACT_KEY, f.R_ENTRY_VERTICAL} <= names
    for spec in schema.FIELDS:
        assert schema.api_name_for(spec.body["field_label"]) == spec.api_name, spec


# --- delivery receipts ----------------------------------------------------------------------------------
@pytest.mark.parametrize("body", [
    {"messageId": "wamid.1", "status": "delivered"},
    {"data": {"id": "wamid.1", "status": "DELIVERED"}},
    {"statuses": [{"id": "wamid.1", "status": "delivered"}]},
    {"whatsapp": {"statuses": [{"id": "wamid.1", "status": "delivered"}]}},
])
def test_a_receipt_is_read_in_the_usual_shapes(body):
    assert delivery.read(body) == ("wamid.1", "delivered")


async def test_receipts_update_the_send_and_never_go_backwards(session):
    campaign, customer = await ready(session)
    await run(session, Gallabox())
    send = (await session.execute(select(NudgeSend))).scalar_one()
    assert send.provider_message_id == "wamid.1"
    assert await delivery.record(session, {"messageId": "wamid.1", "status": "read"})
    assert await delivery.record(session, {"messageId": "wamid.1", "status": "delivered"})
    assert send.delivery_status == "read"                      # a late "delivered" is ignored
    assert not await delivery.record(session, {"messageId": "someone-else", "status": "read"})
    assert not await delivery.record(session, {"nothing": "here"})
    stats = await reports.stats(session, days=30)
    assert (stats["messages"][0]["delivered"], stats["messages"][0]["read"]) == (1, 1)


# --- the dashboard -------------------------------------------------------------------------------------
async def test_the_dashboard_counts_buckets_flow_and_dead_leads(session):
    campaign, customer = await ready(session)
    await run(session, Phone())
    await followup.flow_started(session, customer, "ORDER FOOD")
    other = await prospect(session, "447700900888")
    await journey_at(session, campaign, other, started_days_ago=184, attempts=12)
    await run(session, Phone())
    await optout.hold(session, await prospect(session, "447700900777"), "support", "", actor="ops")
    d = await reports.smo_dashboard(session)
    assert d["journeys"]["by_crm_bucket"] == {"Engaged": 1, "Dead Lead": 1}
    assert d["journeys"]["flow"] == {"active": 1}
    assert d["journeys"]["end_reasons"] == {"length_reached": 1}
    assert d["suppressed"]["holds"] == {"support": 1}
    assert d["sends"]["sent"] == 1


# --- the message library's occasion columns --------------------------------------------------------------
async def test_order_by_and_kitchens_go_in_and_out_of_a_file(session):
    sheet = ("ID,Title,Message,From,Order by,Until,Kitchens\n"
             "FEST-1,Diwali,Send ORDER FOOD,2026-11-01,2026-11-07,2026-11-09,\"edison, jc\"\n")
    await library.import_rows(session, library.read_file("x.csv", sheet.encode()), commit=True,
                              actor="test")
    message = await library.by_code(session, "FEST-1")
    assert (message.order_by, message.kitchens) == (date(2026, 11, 7), ["EDISON", "JC"])
    out, _ = library.export([message], "json")
    assert b'"order_by": "2026-11-07"' in out and b'"EDISON"' in out
    with pytest.raises(cfg.NudgeConfigError, match="Order by is before From"):
        library.clean({"code": "X", "title": "x", "body": "ORDER FOOD", "starts_on": "2026-11-05",
                       "order_by": "2026-11-01"})


# --- what the page offers ---------------------------------------------------------------------------------
def test_only_the_smo_journey_and_recovery_can_be_made():
    reference = cfg.reference()
    assert reference["active_kinds"] == ["follow_up", "abandoned_cart", "abandoned_checkout"]
    for kind in ("repeat_purchase", "never_ordered", "targeted_offer"):
        with pytest.raises(cfg.NudgeConfigError):
            cfg.parse_active_kind(kind)


async def test_a_paid_order_is_not_a_prospect(session):
    campaign, customer = await ready(session)
    await order(session, customer, days_ago=1, status=PaymentStatus.PAID)
    report = await run(session, Phone())
    assert UK not in {o.whatsapp_number for o in report.outcomes if o.status == "sent"}
    assert (await session.execute(select(NudgeJourney))).first() is None
    assert JourneyStatus.ACTIVE and FlowStatus.ACTIVE and MenuItem


# --- the API and the page ----------------------------------------------------------------------------------
@pytest.fixture
def api(session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes.nudges import nudge_actor, router
    from app.db.session import get_session

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[nudge_actor] = lambda: "ops@test"
    return TestClient(app)


async def test_the_api_holds_refuses_retired_types_and_shows_the_dashboard(api, session):
    customer = await prospect(session)
    refused = api.post("/api/nudges/campaigns", json={"name": "Win-back", "kind": "repeat_purchase"})
    assert refused.status_code == 422 and "follow_up" in refused.json()["detail"]
    made = api.post("/api/nudges/campaigns", json={"name": "SMO", "kind": "follow_up"})
    assert made.status_code == 201
    assert api.patch(f"/api/nudges/campaigns/{made.json()['id']}",
                     json={"kind": "targeted_offer"}).status_code == 422

    held = api.post(f"/api/nudges/customers/{UK}/hold", json={"hold": "complaint", "note": "Refund"})
    assert held.json() == {"hold": "complaint"}
    detail = api.get(f"/api/nudges/customers/{UK}").json()
    assert (detail["hold"], detail["hold_label"], detail["hold_note"]) == ("complaint", "Complaint", "Refund")
    assert api.post(f"/api/nudges/customers/{UK}/hold", json={"hold": "nope"}).status_code == 422
    assert api.post(f"/api/nudges/customers/{UK}/release").json()["changed"] is True
    assert customer.nudge_hold is None

    dashboard = api.get("/api/nudges/dashboard").json()
    assert {"journeys", "deferred", "passed_over", "sends", "suppressed"} <= set(dashboard)
    assert api.get("/api/nudges/reference").json()["active_kinds"] == [
        "follow_up", "abandoned_cart", "abandoned_checkout"]


def test_the_page_has_no_settings_the_handoff_does_not_ask_for():
    from pathlib import Path

    page = Path("app/templates/admin/nudges.html").read_text(encoding="utf-8")
    for gone in ("Frequency limits", "Who gets it", "Advanced: edit as JSON", "data-path=\"priority\"",
                 "No coupon</option>", "attribution_days', 'Count an order"):
        assert gone not in page, gone
    for there in ("activeKinds()", "smoDashboard", "holdbtn", "avoid_angle_repeat",
                  "resume_after_days", "data-mkitchen", "order_by"):
        assert there in page, there
