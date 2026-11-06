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
    assert written[0][2][f.BOT_STAGE] == written[0][2][f.LEAD_STATUS] == CART
    assert written[-1][2][f.R_BUCKET] == "No-response"
    assert written[-1][2][f.BOT_STAGE] == "No Response"         # the bucket is their Zoho stage
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


def test_which_whatsapp_templates_can_be_added():
    body = {"type": "BODY", "text": "Rice is sorted? Send ORDER FOOD on WhatsApp."}
    row = {"name": "smo_m1_01", "status": "APPROVED", "category": "MARKETING", "components": [body]}
    assert messages.from_template(row) == {"name": "smo_m1_01", "text": body["text"],
                                           "buttons": [], "source": "whatsapp"}
    replies = {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Order Food"}]}
    assert messages.from_template({**row, "components": [body, replies]})["buttons"] == ["Order Food"]
    link = {"type": "BUTTONS", "buttons": [{"type": "URL", "text": "Order", "url": "https://x/{{1}}"}]}
    for refused in ({**row, "components": [body, link]},                       # needs a link value
                    {**row, "components": [{"type": "BODY", "text": "Hi {{1}}, hungry?"}]},
                    {**row, "status": "PENDING"}, {**row, "category": "UTILITY"}):
        assert messages.from_template(refused) is None


async def test_a_message_from_whatsapp_goes_without_buttons(session, customer, phone):
    data = (await switch_on(session)).model_dump()
    data["messages"].append({"name": "smo_m1_01", "text": "Rice is sorted? Send ORDER FOOD on WhatsApp.",
                             "buttons": [], "source": "whatsapp"})
    for rule in data["rules"]:
        if rule["stage"] == CART:
            rule["message"] = "smo_m1_01"
    await config.save(session, data, "test")
    at_stage(customer, CART, hours_ago=5)

    await runner.run(session, NOW)
    assert phone.nudges() == ["smo_m1_01"]
    assert phone.sent[-1].payload == {"values": [], "buttons": ()}
    assert messages.spec(Message(name="smo_m1_01", text="Rice is sorted? Send ORDER FOOD.",
                                 buttons=[])).button_kind == "none"


# --- Zoho first ------------------------------------------------------------------------------
@pytest.fixture
def zoho_says(monkeypatch, customer):
    """What the customer's Lead reads in Zoho; writes to Zoho are dropped."""
    customer.zoho_lead_id = "L1"
    record = {}

    async def get_record(module, record_id):
        if isinstance(record.get("raise"), Exception):
            raise record["raise"]
        return dict(record)

    async def quiet(*args, **kwargs):
        return None

    monkeypatch.setattr(crm.crm, "get_record", get_record)
    monkeypatch.setattr(crm.crm, "update_record", quiet)
    monkeypatch.setattr(crm.engagement, "add_note", quiet)
    return record


async def test_the_teams_stage_in_zoho_wins(session, customer, phone, zoho_says):
    await switch_on(session)
    at_stage(customer, CART, hours_ago=5)
    zoho_says.update({f.BOT_STAGE: "Later", f.LEAD_STATUS: "Later"})       # moved by the team
    await runner.run(session, NOW)
    lead = await lead_of(session, customer)
    assert phone.nudges() == [] and lead.bucket == NudgeBucket.LATER
    assert lead.waiting == "Zoho shows Later: following Zoho"

    zoho_says.update({f.LEAD_STATUS: "Junk Lead"})
    await runner.run(session, NOW + timedelta(days=8))
    lead = await lead_of(session, customer)
    assert phone.nudges() == [] and lead.bucket == NudgeBucket.DEAD
    assert lead.waiting == "Zoho shows Junk Lead: no more nudges"


async def test_a_stale_ordering_stage_in_zoho_does_not_stop_a_send(session, customer, phone,
                                                                    zoho_says):
    await switch_on(session)
    at_stage(customer, CART, hours_ago=5)
    zoho_says.update({f.BOT_STAGE: "Cuisine Selected"})       # the bot's last update didn't land
    await runner.run(session, NOW)
    assert phone.nudges() == ["shero_nudge_cart"]


async def test_no_send_while_zoho_cannot_be_read(session, customer, phone, zoho_says):
    await switch_on(session)
    at_stage(customer, CART, hours_ago=5)
    zoho_says["raise"] = IntegrationError("zoho", "503 busy")
    await runner.run(session, NOW)
    assert phone.nudges() == []
    assert (await lead_of(session, customer)).waiting.startswith("Couldn't read the stage in Zoho")


async def test_zoho_is_set_up_once_a_day_and_keeps_trying_the_fields(session, monkeypatch):
    calls = {"options": [], "create": 0, "list": 0}
    live = {f.LEADS: [{"api_name": f.BOT_STAGE, "id": "b1", "pick_list_values": [{"display_value": CART}]},
                      {"api_name": f.LEAD_STATUS, "id": "s1", "pick_list_values": []}],
            f.CONTACTS: [{"api_name": f.BOT_STAGE, "id": "b2",
                          "pick_list_values": [{"display_value": "Later"}]}]}

    async def list_fields(module):
        calls["list"] += 1
        return live[module]

    async def add_picklist_options(module, field_id, options):
        calls["options"].append((module, field_id, options))

    async def create_fields(module, fields):
        calls["create"] += 1
        raise IntegrationError("zoho", "field create failed - 'Retargeting Bucket': LIMIT_EXCEEDED "
                                       "Field limit reached for the given field type")

    async def get_module(name):
        return None                                 # no Nudge Messages module yet

    async def create_module(name, singular, plural):
        calls["module"] = calls.get("module", 0) + 1
        raise IntegrationError("zoho", "module create failed: NOT_SUPPORTED "
                                       "the feature is not available for this edition")

    for name, fake in (("list_fields", list_fields), ("add_picklist_options", add_picklist_options),
                       ("create_fields", create_fields), ("get_module", get_module),
                       ("create_module", create_module)):
        monkeypatch.setattr(crm.zoho_client, name, fake)

    result = await crm.ensure(session)
    assert calls["options"] == [
        (f.LEADS, "b1", ["No Response", "Later", "No", "Dead Lead"]),
        (f.LEADS, "s1", ["No Response", "Later", "No", "Dead Lead"]),
        (f.CONTACTS, "b2", ["No Response", "No", "Dead Lead"])]
    assert len(result["fields_missing"]) == 10 and "custom field limit" in result["error"]
    assert not result["module_ready"] and "not available on this Zoho plan" in result["module_error"]
    assert (await crm.setup_status(session))["fields_missing"] == result["fields_missing"]

    await crm.ensure(session)                       # within the day: nothing asked
    assert calls["create"] == 2 and calls["module"] == 1
    await crm.ensure(session, force=True)           # Try now
    assert calls["create"] == 4 and calls["module"] == 2


async def test_after_the_upgrade_the_module_and_its_fields_are_created(session, monkeypatch):
    """A plan that allows it: the module, then its fields five at a time."""
    from app.integrations.zoho import schema

    created: dict = {"module": [], "fields": []}
    live: dict = {f.LEADS: [], f.CONTACTS: [], crm.MODULE: []}

    async def get_module(name):
        return {"api_name": name} if created["module"] else None

    async def create_module(name, singular, plural):
        created["module"].append((name, singular, plural))

    async def list_fields(module):
        return live[module]

    async def create_fields(module, bodies):
        assert len(bodies) <= 5
        created["fields"].append((module, [b["field_label"] for b in bodies]))
        live[module] += [{"api_name": schema.api_name_for(b["field_label"])} for b in bodies]

    async def nothing(*args, **kwargs):
        return None

    for name, fake in (("get_module", get_module), ("create_module", create_module),
                       ("list_fields", list_fields), ("create_fields", create_fields),
                       ("add_picklist_options", nothing)):
        monkeypatch.setattr(crm.zoho_client, name, fake)

    result = await crm.ensure(session, force=True)
    assert created["module"] == [("Nudge_Messages", "Nudge Message", "Nudge Messages")]
    labels = [label for module, batch in created["fields"] if module == crm.MODULE for label in batch]
    assert labels[:2] == ["Lead", "Contact"] and len(labels) == len(crm.MODULE_FIELDS) == 14
    assert result["module_ready"] and not result["fields_missing"] and not result["module_error"]


# --- Nudge Messages: a record per message --------------------------------------------------
@pytest.fixture
def nudge_records(monkeypatch, zoho_says):
    """The Nudge Messages module is ready; records written to it are kept."""
    written: dict = {"create": [], "update": []}

    async def ready(session, *, force=False):
        return {"module_ready": True}

    async def create_record(module, fields):
        written["create"].append((module, dict(fields)))
        return f"N{len(written['create'])}"

    async def update_record(module, record_id, fields):
        written["update"].append((module, record_id, dict(fields)))
        return record_id

    monkeypatch.setattr(crm, "ensure", ready)
    monkeypatch.setattr(crm.zoho_client, "create_record", create_record)
    monkeypatch.setattr(crm.zoho_client, "update_record", update_record)
    return written


async def test_each_nudge_is_a_record_under_the_lead(session, customer, phone, nudge_records):
    await switch_on(session)
    at_stage(customer, CART, hours_ago=5)
    await runner.run(session, NOW)

    [(module, fields)] = nudge_records["create"]
    assert module == "Nudge_Messages"
    assert fields[f.N_LEAD] == {"id": "L1"} and f.N_CONTACT not in fields
    assert (fields[f.N_NAME], fields[f.N_SEQUENCE], fields[f.N_TEMPLATE], fields[f.N_ATTEMPT]) == (
        "Cart Created - stage message", "Stage message", "shero_nudge_cart", 1)
    assert (fields[f.N_SEND_STATUS], fields[f.N_WHATSAPP_ID]) == ("Sent", "wamid.1")
    [row] = await sends(session)
    assert row.zoho_id == "N1"


async def test_delivery_and_the_button_tapped_are_written_back(session, customer, phone,
                                                                 nudge_records):
    await switch_on(session)
    at_stage(customer, CART, hours_ago=5)
    await runner.run(session, NOW)

    await delivery.record(session, {"statuses": [{"id": "wamid.1", "status": "read"}]})
    [(module, record_id, fields)] = nudge_records["update"]
    assert (module, record_id, fields[f.N_DELIVERY]) == ("Nudge_Messages", "N1", "Read")

    await handle_event(session, tap("Later"))
    reply = nudge_records["update"][-1]
    assert reply[1] == "N1" and reply[2][f.N_REPLY] == "Later" and f.N_REPLY_TIME in reply[2]


async def test_a_failed_send_is_a_record_too(session, customer, phone, nudge_records):
    await switch_on(session)
    at_stage(customer, CART, hours_ago=5)
    phone.fail = "131026 not a WhatsApp number"
    await runner.run(session, NOW)
    [(module, fields)] = nudge_records["create"]
    assert fields[f.N_SEND_STATUS] == "Failed" and "131026" in fields[f.N_SEND_ERROR]


async def test_a_field_zoho_lacks_is_left_out_but_the_record_still_goes(session, customer,
                                                                          monkeypatch):
    customer.zoho_contact_id = "C9"
    payloads = []

    async def create_record(module, fields):
        payloads.append(dict(fields))
        if f.N_DELIVERY in fields or f.N_WHATSAPP_ID in fields:
            raise IntegrationError("zoho", "record write failed: INVALID_DATA invalid data",
                                   payload={"code": "INVALID_DATA",
                                            "details": {"api_name": f.N_WHATSAPP_ID}})
        return "N7"

    monkeypatch.setattr(crm.zoho_client, "create_record", create_record)
    send = NudgeSend(customer_id=customer.id, kind=NudgeBucket.LATER, stage=CART, attempt=3,
                     template="shero_nudge_back", status="sent", sent_at=NOW,
                     provider_message_id="wamid.9")
    await crm.file_message(customer, send)
    assert send.zoho_id == "N7" and payloads[-1][f.N_CONTACT] == {"id": "C9"}
    assert f.N_WHATSAPP_ID not in payloads[-1] and payloads[-1][f.N_NAME] == "Later - message 3"


async def test_a_new_contact_gets_the_leads_messages(session, customer, monkeypatch):
    updates = []

    async def update_record(module, record_id, fields):
        updates.append((record_id, fields))

    monkeypatch.setattr(crm.zoho_client, "update_record", update_record)
    for n, zoho_id in enumerate(("N1", None, "N3")):
        session.add(NudgeSend(customer_id=customer.id, kind="stage", stage=CART, attempt=1,
                              template="shero_nudge_cart", status="sent",
                              sent_at=NOW + timedelta(minutes=n), zoho_id=zoho_id))
    await session.flush()
    customer.zoho_contact_id = "C5"
    await crm.relink(session, customer)
    assert sorted(updates) == [("N1", {f.N_CONTACT: {"id": "C5"}}),
                               ("N3", {f.N_CONTACT: {"id": "C5"}})]
