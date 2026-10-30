"""Test mode (Settings > Test mode): only the test numbers get nudges, they
always count for the follow-up journey, and Send now (runner.send_now) gives
one of them a campaign's next message without the waiting."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.models import NudgeSend, NudgeStatus
from app.integrations.gallabox.sender import use_sender
from app.services.nudges import followup, optout, runner
from app.services.nudges.config import NudgeConfigError
from tests.services.test_nudge_followups import (UK, add_messages, journey_campaign, prospect,
                                                 the_journey)
from tests.services.test_nudges import (Phone, left_cart, make, now, order, reasons, run,
                                        switch_on, wrote_in)

pytestmark = pytest.mark.asyncio

OTHER = "447700900456"


async def testing(session, **settings):
    """Test mode with UK as the test number; the shop-wide limits as shipped."""
    await switch_on(session, test_numbers=[UK], **settings)
    await add_messages(session)
    return await journey_campaign(session)


async def send_now(session, campaign, customer, phone):
    with use_sender(phone):
        return await runner.send_now(session, campaign, customer, actor="ops@test")


async def test_only_test_numbers_are_nudged_and_one_counts_whatever_its_orders(session):
    await testing(session)
    tester = await prospect(session, UK, days=0)            # wrote in today, ordered before
    await order(session, tester, days_ago=20)
    await prospect(session, OTHER, name="Ravi")             # a real prospect, due
    phone = Phone()

    report = await run(session, phone)

    assert [m["to"] for m in phone.sent] == [UK]
    assert reasons(report)[OTHER] == "not_a_test_number"
    assert (await the_journey(session)).customer_id == tester.id


async def test_send_now_skips_the_waiting_and_moves_the_journey_on(session):
    campaign = await testing(session, send_window={"start": "03:00", "end": "03:01"})
    tester = await prospect(session, UK, days=0)
    phone = Phone()

    results = [await send_now(session, campaign, tester, phone) for _ in range(3)]

    # Three in a row: no 3-day wait, no once-a-day limit, no quiet hours.
    assert [r["sent"] for r in results] == [True, True, True]
    journey = await the_journey(session)
    assert journey.attempts == 3 and len({r["message"] for r in results}) == 3
    # The scheduler still keeps the real timing for them.
    assert reasons(await run(session, phone))[UK] in ("not_due", "quiet_hours")
    assert len(phone.sent) == 3


async def test_send_now_is_for_test_numbers_only(session):
    campaign = await testing(session)
    other = await prospect(session, OTHER)
    with pytest.raises(NudgeConfigError, match="test numbers only"):
        await send_now(session, campaign, other, Phone())


async def test_stop_and_holds_still_apply(session):
    campaign = await testing(session)
    tester = await prospect(session, UK)
    await optout.opt_out(session, tester, source="test")
    result = await send_now(session, campaign, tester, Phone())
    assert (result["sent"], result["reason"]) == (False, "opted_out")
    assert "START OFFERS" in result["hint"]


async def test_an_order_paid_during_the_test_ends_the_journey_and_restart_tries_again(session):
    campaign = await testing(session)
    tester = await prospect(session, UK)
    await order(session, tester, days_ago=30)               # before the test: ignored
    phone = Phone()
    assert (await send_now(session, campaign, tester, phone))["sent"]

    await order(session, tester, days_ago=0)
    (await the_journey(session)).started_at = now() - timedelta(hours=1)
    result = await send_now(session, campaign, tester, phone)
    assert (result["sent"], result["reason"]) == (False, "journey_converted")

    await followup.act(session, await the_journey(session), "restart", actor="ops@test")
    assert (await send_now(session, campaign, tester, phone))["sent"]


async def test_order_food_then_send_now_counts_the_flow_abandoned(session):
    campaign = await testing(session)
    tester = await prospect(session, UK)
    phone = Phone()
    await send_now(session, campaign, tester, phone)
    await followup.flow_started(session, tester, "ORDER FOOD")
    assert reasons(await run(session, phone))[UK] == "in_ordering_flow"

    result = await send_now(session, campaign, tester, phone)

    assert result["sent"] and "abandoned" in result["notes"][0]
    assert (await the_journey(session)).flow_status == "abandoned"


async def test_a_left_cart_without_the_hour_wait_and_never_twice(session):
    await switch_on(session, test_numbers=[UK])
    cart_campaign = await make(session, "abandoned_cart")
    tester = await prospect(session, UK)
    phone = Phone()

    empty = await send_now(session, cart_campaign, tester, phone)
    assert empty["reason"] == "trigger_not_met" and "cart is empty" in empty["hint"]

    await left_cart(session, tester, minutes_ago=1)         # the reminder waits an hour
    await wrote_in(session, tester, hours_ago=0.1)          # a normal message: the 24-hour window
    assert (await send_now(session, cart_campaign, tester, phone))["sent"]
    again = await send_now(session, cart_campaign, tester, phone)
    assert again["reason"] == "already_sent"
    rows = (await session.execute(select(NudgeSend))).scalars().all()
    assert [r.status for r in rows] == [NudgeStatus.SENT]


async def test_dry_run_logs_without_sending(session):
    campaign = await testing(session, dry_run=True)
    tester = await prospect(session, UK)
    phone = Phone()
    result = await send_now(session, campaign, tester, phone)
    assert result["status"] == "dry_run" and phone.sent == []
    assert "Dry run" in result["notes"][0]


# --- the API and the page --------------------------------------------------------------------
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


async def test_the_api_marks_a_test_number_and_sends_now(api, session):
    campaign = await testing(session)
    await prospect(session, UK)
    await prospect(session, OTHER, name="Ravi")
    assert api.get(f"/api/nudges/customers/{UK}").json()["test_number"] is True
    assert api.get(f"/api/nudges/customers/{OTHER}").json()["test_number"] is False

    with use_sender(Phone()):
        sent = api.post(f"/api/nudges/customers/{UK}/send-now",
                        json={"campaign_id": str(campaign.id)})
    assert sent.status_code == 200 and sent.json()["sent"] is True
    refused = api.post(f"/api/nudges/customers/{OTHER}/send-now",
                       json={"campaign_id": str(campaign.id)})
    assert refused.status_code == 422
    assert api.post(f"/api/nudges/customers/{UK}/send-now",
                    json={"campaign_id": "nope"}).status_code == 404


def test_the_page_has_test_mode():
    from pathlib import Path

    page = Path("app/templates/admin/nudges.html").read_text(encoding="utf-8")
    for there in ("card('Test mode'", "testBanner(r)", "data-sendnow", "/send-now"):
        assert there in page, there
