"""Nudges by stage: the rule's message after the wait, the move to No response,
the No / Later / No response sequences to Dead Lead, and the customer's
answers (services/nudges)."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.exceptions import IntegrationError
from app.db.models import (ConversationStep, LeadStage, NudgeBucket, NudgeLead, NudgeSend)
from app.integrations.gallabox.sender import use_sender
from app.integrations.zoho import fields as f
from app.schemas.inbound import InboundEvent, InboundKind
from app.services import reply_triggers
from app.services.conversation.engine import handle_event
from app.services.customers import get_or_create_conversation
from app.services.nudges import config, crm, delivery, messages, runner
from app.services.nudges.config import Message, NudgeConfigError, NudgeSettings
from tests.conversation.test_happy_path import FakeGallabox, SentMessage

# Mid-morning in New York (15:00 UTC), inside the default 9:00-21:00 send hours,
# and close to the real clock so a reply finds the nudge it answers.
NOW = datetime.now(timezone.utc).replace(hour=15, minute=0, second=0, microsecond=0)
CART = LeadStage.CART_CREATED.value
TEXT = "Hi {name}, your Shero meal is still in your cart. Would you like it today?"


class Phone(FakeGallabox):
    """Records nudges; `fail` makes the next sends fail."""

    def __init__(self):
        super().__init__()
        self.count, self.fail = 0, None

    async def send_template_only(self, to, spec, *values, button_value=None):
        if self.fail:
            raise IntegrationError("gallabox", self.fail)
        self.count += 1
        self.sent.append(SentMessage("nudge", to, spec.name,
                                     {"values": list(values), "buttons": spec.quick_replies}))
        return {"id": f"wamid.{self.count}", "status": "accepted"}

    def nudges(self) -> list[str]:
        return [m.body for m in self.sent if m.kind == "nudge"]


@pytest.fixture
def phone():
    fake = Phone()
    with use_sender(fake):
        yield fake


async def switch_on(session, **changes) -> NudgeSettings:
    data = NudgeSettings(messages=[
        Message(name="shero_nudge_cart", text=TEXT),
        Message(name="shero_nudge_back", text="We miss you at Shero. Fancy a home-cooked meal today?"),
    ]).model_dump()
    data["enabled"] = True
    for rule in data["rules"]:
        if rule["stage"] == CART:
            rule.update(active=True, message="shero_nudge_cart", wait_hours=3, no_response_hours=24)
    for seq in data["sequences"].values():
        seq.update(active=True, first_message="shero_nudge_back")
    data.update(changes)
    return await config.save(session, data, "test")


def at_stage(customer, stage: str, *, hours_ago: float, now: datetime = NOW) -> None:
    customer.lead_stage = stage
    customer.stage_timestamps = {**(customer.stage_timestamps or {}),
                                 stage: (now - timedelta(hours=hours_ago)).isoformat()}


async def lead_of(session, customer) -> NudgeLead | None:
    return (await session.execute(select(NudgeLead).where(
        NudgeLead.customer_id == customer.id))).scalar_one_or_none()


async def sends(session) -> list[NudgeSend]:
    return list((await session.execute(select(NudgeSend).order_by(NudgeSend.sent_at))).scalars())


async def in_bucket(session, customer, bucket: str, at: datetime = NOW) -> NudgeLead:
    lead = NudgeLead(customer_id=customer.id, stage=customer.lead_stage, bucket=bucket,
                     bucket_from=customer.lead_stage, bucket_at=at, attempts=0, failures=0,
                     stage_entered_at=at - timedelta(days=1), stage_sent_at=at - timedelta(hours=20))
    session.add(lead)
    await session.flush()
    return lead


# --- a stage's message ----------------------------------------------------------------
async def test_a_stage_message_goes_after_the_wait_once(session, customer, phone):
    await switch_on(session)
    at_stage(customer, CART, hours_ago=2)

    await runner.run(session, NOW)
    lead = await lead_of(session, customer)
    assert phone.nudges() == [] and runner.aware(lead.next_due_at) == NOW + timedelta(hours=1)

    await runner.run(session, NOW + timedelta(hours=1, minutes=1))
    await runner.run(session, NOW + timedelta(hours=2))
    assert phone.nudges() == ["shero_nudge_cart"]                  # once for this stage entry
    sent = phone.sent[-1].payload
    assert sent["values"] == ["Asha"] and sent["buttons"] == ("Order Food", "Later", "No")
    [row] = await sends(session)
    assert (row.kind, row.stage, row.status, row.provider_message_id) == ("stage", CART, "sent", "wamid.1")


async def test_no_answer_moves_them_to_no_response_and_its_sequence_runs(session, customer, phone):
    await switch_on(session)
    at_stage(customer, CART, hours_ago=4)
    await runner.run(session, NOW)

    await runner.run(session, NOW + timedelta(hours=25))
    lead = await lead_of(session, customer)
    assert lead.bucket == NudgeBucket.NO_RESPONSE and lead.bucket_from == CART
    assert runner.aware(lead.next_due_at) == NOW + timedelta(hours=49)          # its first message: 24 hours in

    for days in (49 / 24, 49 / 24 + 1, 49 / 24 + 2, 49 / 24 + 12):
        await runner.run(session, NOW + timedelta(days=days))
    assert phone.nudges() == ["shero_nudge_cart"] + ["shero_nudge_back"] * 4
    assert (await lead_of(session, customer)).attempts == 4


async def test_moving_to_another_stage_ends_the_bucket(session, customer, phone):
    await switch_on(session)
    at_stage(customer, CART, hours_ago=30)
    await in_bucket(session, customer, NudgeBucket.LATER)

    at_stage(customer, LeadStage.SLOT_SELECTED.value, hours_ago=0.1)
    await runner.run(session, NOW)
    lead = await lead_of(session, customer)
    assert lead.bucket is None and lead.stage == LeadStage.SLOT_SELECTED.value


async def test_paying_stops_everything(session, customer, phone):
    await switch_on(session)
    at_stage(customer, CART, hours_ago=30)
    await in_bucket(session, customer, NudgeBucket.NO_RESPONSE, NOW - timedelta(days=3))
    at_stage(customer, LeadStage.CONVERTED.value, hours_ago=1)

    await runner.run(session, NOW)
    lead = await lead_of(session, customer)
    assert lead.bucket is None and lead.next_due_at is None and phone.nudges() == []


# --- the sequences ------------------------------------------------------------------------
async def test_later_follows_its_phases_until_dead_lead(session, customer, phone):
    await switch_on(session)
    at_stage(customer, CART, hours_ago=30)
    await in_bucket(session, customer, NudgeBucket.LATER)

    for days in (6, 7, 17, 27, 57, 87):
        await runner.run(session, NOW + timedelta(days=days))
    assert len(phone.nudges()) == 5                              # day 7, +10, +10, +30, +30
    await runner.run(session, NOW + timedelta(days=181))
    assert (await lead_of(session, customer)).bucket == NudgeBucket.DEAD
    await runner.run(session, NOW + timedelta(days=400))
    assert len(phone.nudges()) == 5


async def test_no_sends_one_message_then_dead_lead(session, customer, phone):
    await switch_on(session)
    at_stage(customer, CART, hours_ago=30)
    await in_bucket(session, customer, NudgeBucket.NO)

    await runner.run(session, NOW + timedelta(days=29))
    await runner.run(session, NOW + timedelta(days=30))
    await runner.run(session, NOW + timedelta(days=30, minutes=5))
    assert phone.nudges() == ["shero_nudge_back"]
    assert (await lead_of(session, customer)).bucket == NudgeBucket.DEAD


# --- when nothing goes --------------------------------------------------------------------
async def test_nothing_goes_while_switched_off(session, customer, phone):
    await switch_on(session, enabled=False)
    at_stage(customer, CART, hours_ago=5)
    assert await runner.run(session, NOW) == {"off": True}
    assert phone.nudges() == [] and await lead_of(session, customer) is None


async def test_it_waits_for_the_send_hours_and_for_approval(session, customer, phone, monkeypatch):
    await switch_on(session)
    at_stage(customer, CART, hours_ago=5, now=NOW + timedelta(hours=12))
    await runner.run(session, NOW + timedelta(hours=12))          # 22:00 or 23:00 in New York
    assert phone.nudges() == []
    assert (await lead_of(session, customer)).waiting.startswith("Waits for the send hours")

    async def not_yet(name):
        return False

    monkeypatch.setattr(messages.template_status, "is_approved", not_yet)
    await runner.run(session, NOW + timedelta(days=1))
    assert (await lead_of(session, customer)).waiting == "shero_nudge_cart is not approved by WhatsApp yet"
    assert phone.nudges() == []


async def test_not_while_ordering_and_never_after_stop(session, customer, phone):
    await switch_on(session)
    at_stage(customer, CART, hours_ago=5)
    conversation = await get_or_create_conversation(session, customer)
    conversation.step = ConversationStep.AWAIT_QUANTITY
    conversation.last_message_at = NOW - timedelta(minutes=10)
    await runner.run(session, NOW)
    assert (await lead_of(session, customer)).waiting == "Ordering with the bot right now"

    conversation.step = ConversationStep.COMPLETED
    customer.nudges_opted_out_at = NOW - timedelta(days=1)
    await runner.run(session, NOW + timedelta(hours=1))
    assert phone.nudges() == []


async def test_a_stage_reached_long_ago_is_left_alone(session, customer, phone):
    await switch_on(session)
    at_stage(customer, CART, hours_ago=24 * 30)
    await runner.run(session, NOW)
    assert phone.nudges() == [] and await lead_of(session, customer) is None


async def test_a_failing_send_is_retried_then_given_up(session, customer, phone):
    await switch_on(session)
    at_stage(customer, CART, hours_ago=5)
    phone.fail = "422 not a WhatsApp number"
    for minutes in (0, 5, 10):
        await runner.run(session, NOW + timedelta(minutes=minutes))
        lead = await lead_of(session, customer)
    rows = await sends(session)
    assert [r.status for r in rows] == ["failed"] * 3 and "422" in rows[0].error
    assert lead.stage_sent_at is not None and lead.waiting.startswith("Gave up after 3")


# --- the customer's answer ---------------------------------------------------------------
def tap(title: str) -> InboundEvent:
    return InboundEvent(whatsapp_number="17325550142", message_id=f"tap-{title}-{datetime.now().timestamp()}",
                        kind=InboundKind.REPLY, reply_id=title, reply_title=title)


async def nudged(session, customer, phone):
    await switch_on(session)
    at_stage(customer, CART, hours_ago=5)
    await runner.run(session, NOW)
    assert phone.nudges() == ["shero_nudge_cart"]


async def test_tapping_later_moves_them_to_later(session, customer, phone):
    await nudged(session, customer, phone)
    await handle_event(session, tap("Later"))

    lead = await lead_of(session, customer)
    assert lead.bucket == NudgeBucket.LATER and lead.last_reply == "Later"
    assert lead.next_due_at - lead.bucket_at == timedelta(days=7)
    assert phone.last().body.startswith("No problem! We'll check back")
    assert (await sends(session))[0].reply == "Later"


async def test_tapping_no_and_replying_stop(session, customer, phone):
    await nudged(session, customer, phone)
    await handle_event(session, tap("No"))
    assert (await lead_of(session, customer)).bucket == NudgeBucket.NO

    await handle_event(session, InboundEvent(whatsapp_number="17325550142", message_id="stop-1",
                                             kind=InboundKind.TEXT, text="STOP"))
    lead = await lead_of(session, customer)
    assert lead.bucket == NudgeBucket.DEAD and customer.nudges_opted_out_at is not None
    assert "won't get these messages" in phone.last().body


async def test_order_food_ends_the_bucket_and_starts_an_order(session, customer, phone):
    await nudged(session, customer, phone)
    await handle_event(session, tap("Later"))
    await handle_event(session, tap("Order Food"))

    lead = await lead_of(session, customer)
    assert lead.bucket is None and lead.last_reply == "Order Food"
    assert phone.last().kind != "nudge"                          # the bot carried on with the order


async def test_no_is_only_an_answer_after_a_nudge(session, customer, phone):
    await switch_on(session)
    await handle_event(session, tap("No"))
    assert await lead_of(session, customer) is None
    assert not await reply_triggers._answered_anyway(session, "17325550142", "later")

    at_stage(customer, CART, hours_ago=5)
    await runner.run(session, NOW)
    assert await reply_triggers._answered_anyway(session, "17325550142", "later")


# --- receipts and Zoho --------------------------------------------------------------------
async def test_a_receipt_marks_the_message_read(session, customer, phone):
    await nudged(session, customer, phone)
    assert await delivery.record(session, {"statuses": [{"id": "wamid.1", "status": "read"}]})
    assert (await sends(session))[0].delivery_status == "read"


async def test_zoho_gets_the_bucket_and_a_note(session, customer, phone, monkeypatch):
    customer.zoho_lead_id = "L1"
    written, notes = [], []

    async def update_record(module, record_id, fields):
        if f.R_LAST_TEMPLATE in fields:     # a CRM without this field
            raise IntegrationError("zoho", "invalid", payload={
                "code": "INVALID_DATA", "details": {"api_name": f.R_LAST_TEMPLATE}})
        written.append((module, record_id, fields))

    async def add_note(module, record_id, *, title, content):
        notes.append(content)

    monkeypatch.setattr(crm.crm, "update_record", update_record)
    monkeypatch.setattr(crm.engagement, "add_note", add_note)
    await nudged(session, customer, phone)
    await runner.run(session, NOW + timedelta(hours=25))

    assert written[0][:2] == (f.LEADS, "L1") and written[0][2][f.R_BUCKET] == "Engaged"
    assert written[-1][2][f.R_BUCKET] == "No-response"
    assert "Sent shero_nudge_cart" in notes[0] and "moved to No response" in notes[-1]
    assert (await lead_of(session, customer)).zoho_error is None


# --- the settings ---------------------------------------------------------------------------
async def test_what_the_settings_refuse():
    with pytest.raises(ValueError, match="choose a message"):
        NudgeSettings.model_validate({"rules": [{"stage": CART, "active": True}]})
    with pytest.raises(ValueError, match="only placeholder"):
        Message(name="shero_nudge_x", text="Your order {order} is waiting for you today")
    with pytest.raises(ValueError, match="start or end"):
        Message(name="shero_nudge_x", text="Your meal is waiting, {name}")
    with pytest.raises(ValueError, match="no such message"):
        NudgeSettings.model_validate({"sequences": {"later": {"first_message": "gone"}}})


async def test_saving_bad_settings_raises_a_readable_error(session):
    with pytest.raises(NudgeConfigError, match="send hours"):
        await config.save(session, {"send_from": 21, "send_to": 9}, "test")


def test_a_message_is_a_template_with_the_three_buttons():
    message = Message(name="shero_nudge_cart", text=TEXT)
    spec = messages.spec(message)
    assert spec.category == "MARKETING" and spec.quick_replies == ("Order Food", "Later", "No")
    assert spec.params == ("customer_name",) and spec.sample_body.startswith("Hi {{1}},")
    assert messages.name_for("Cart reminder!", {"shero_nudge_cart_reminder"}) == "shero_nudge_cart_reminder_2"
