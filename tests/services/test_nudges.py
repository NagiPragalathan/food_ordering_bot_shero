"""Retargeting nudges: the rules, the master switch, sending, STOP, Zoho, stats.

WhatsApp is a fake phone that records what would have been sent, so these
check exactly who gets what, through which channel, and who is left out and
why.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.exceptions import IntegrationError
from app.db.models import (
    ConversationStep,
    Customer,
    DiscountType,
    InboundMessage,
    NudgeSend,
    NudgeStatus,
    Order,
    OrderStage,
    PaymentStatus,
    Placement,
    Promotion,
)
from app.integrations.gallabox.sender import use_sender
from app.schemas.inbound import InboundEvent, InboundKind
from app.services import cart, promotions
from app.services.conversation.context import FlowContext
from app.services.conversation.engine import _handle_global_intent
from app.services.nudges import config as cfg
from app.services.nudges import reports, runner, store
from app.services.nudges.config import NudgeConfigError


def now() -> datetime:
    return datetime.now(timezone.utc)


class Phone:
    """Stands in for Gallabox; `fail` makes every send raise."""

    def __init__(self, fail: bool = False):
        self.sent: list[dict] = []
        self.fail = fail

    def _record(self, **message):
        if self.fail:
            raise IntegrationError("gallabox", "channel down")
        self.sent.append(message)

    async def send_text(self, to, body, **kw):
        self._record(channel="text", to=to, body=body)

    async def send_cta_url(self, to, body, *, url, display_text, **kw):
        self._record(channel="button", to=to, body=body, url=url, label=display_text)

    async def send_template_only(self, to, spec, *values, button_value=None):
        self._record(channel="template", to=to, name=spec.name, values=values,
                     button=button_value)


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "admin_session_secret", "test-secret")


async def switch_on(session, **values):
    """Nudges on, any hour of the day (quiet hours have their own test)."""
    await store.save_settings(session, {"enabled": True,
                                        "send_window": {"start": "00:00", "end": "23:59"},
                                        **values}, actor="test")


async def make(session, kind, *, config=None, code=None, active=True, name=None, priority=0):
    data = {"name": name or f"{kind} test", "kind": kind, "is_active": active,
            "priority": priority, "config": config or {}}
    if code:
        data["promotion_code"] = code
    return await store.create_campaign(session, data, actor="test")


async def left_cart(session, customer, *, minutes_ago=90, lines=None):
    conversation, _ = await cart.for_customer(session, customer)
    cart.write(conversation, lines or [
        {"retailer_id": "sambar", "name": "Drumstick Sambar", "quantity": 2, "unit_price": "12.50"},
        {"retailer_id": "rasam", "name": "Tomato Rasam", "quantity": 1, "unit_price": "9.50"}])
    conversation.set(cart_updated_at=(now() - timedelta(minutes=minutes_ago)).isoformat())
    conversation.last_message_at = now() - timedelta(minutes=minutes_ago)
    await session.flush()
    return conversation


async def wrote_in(session, customer, *, hours_ago=2.0):
    """A message from them: opens WhatsApp's 24-hour window."""
    session.add(InboundMessage(provider_message_id=str(uuid.uuid4()),
                               whatsapp_number=customer.whatsapp_number, payload={},
                               created_at=now() - timedelta(hours=hours_ago)))
    await session.flush()


_numbers = iter(range(1000, 9999))


async def order(session, customer, *, status=PaymentStatus.PAID, days_ago=20, total="30.00",
                stage=OrderStage.DELIVERED, **extra) -> Order:
    at = now() - timedelta(days=days_ago)
    values = {"created_at": at, "paid_at": at if status == PaymentStatus.PAID else None, **extra}
    record = Order(order_number=f"SHO-T-{next(_numbers)}", customer_id=customer.id,
                   payment_status=status, stage=stage, total=Decimal(total),
                   items=[{"retailer_id": "biryani", "name": "Veg Biryani", "quantity": 1}],
                   **values)
    session.add(record)
    await session.flush()
    return record


async def coupon(session, code="COMEBACK", **extra) -> Promotion:
    promo = Promotion(title="Come back", code=code, discount_type=DiscountType.PERCENT,
                      discount_value=Decimal("20"), placement=Placement.HIDDEN,
                      is_active=True, **extra)
    session.add(promo)
    await session.flush()
    return promo


async def run(session, phone=None, **kw):
    with use_sender(phone or Phone()):
        return await runner.run(session, **kw)


def reasons(report) -> dict:
    return {o.whatsapp_number: o.reason for o in report.outcomes if o.status == "skipped"}


async def sends(session) -> list[NudgeSend]:
    return list((await session.execute(select(NudgeSend))).scalars())


# --- configuration --------------------------------------------------------------------------
def test_every_kind_has_working_defaults():
    for kind in cfg.ALL_KINDS:
        config = cfg.build_campaign_config(kind, None, has_coupon=True)
        assert config.message.text


@pytest.mark.parametrize("kind,raw,message", [
    ("abandoned_cart", {"trigger": {"after_minuts": 30}}, "is not an option"),
    ("repeat_purchase", {"trigger": {"after_minutes": 30}}, "do not apply to a repeat_purchase"),
    ("abandoned_cart", {"message": {"text": "Hi {firstname}"}}, "{firstname} is not a placeholder"),
    ("repeat_purchase", {"message": {"text": "Your cart: {cart_items}"}}, "not available for a repeat_purchase"),
    ("abandoned_cart", {"message": {"text": "Use {coupon_code}"}}, "needs a coupon"),
    ("abandoned_cart", {"message": {"text": "Hi {name!r}"}}, "is not a placeholder"),
    ("abandoned_cart", {"message": {"text": "Hi {name"}}, "do not pair up"),
    ("abandoned_cart", {"trigger": {"after_minutes": 600, "within_hours": 2}}, "less than within_hours"),
    ("abandoned_cart", {"message": {"button": "url"}}, "https://"),
    ("abandoned_cart", {"message": {"button_text": "A label far too long for WhatsApp"}}, "20 characters"),
    ("abandoned_cart", {"schedule": {"days": ["funday"]}}, "days are"),
    ("abandoned_cart", {"audience": {"lead_stages": ["Hot"]}}, "is not a lead stage"),
    ("abandoned_cart", {"audience": {"min_paid_orders": 5, "max_paid_orders": 2}}, "leave nobody"),
    ("abandoned_cart", {"message": {"template": {"name": "Bad Name"}}}, "lower case"),
    ("abandoned_cart", {"message": {"template": {"always": True}}}, "needs message.template.name"),
])
def test_mistakes_are_explained(kind, raw, message):
    with pytest.raises(NudgeConfigError, match=message.replace("{", r"\{").replace("}", r"\}")):
        cfg.build_campaign_config(kind, raw, has_coupon=False)


def test_settings_are_checked():
    settings = cfg.build_settings({"test_numbers": ["+1 (732) 555-0142"], "stop_keywords": ["STOP", " Quit  Offers "]})
    assert settings.test_numbers == ["17325550142"]
    assert settings.stop_keywords == ["stop", "quit offers"]
    for raw, message in [({"timezone": "Mars/Base"}, "not a timezone"),
                         ({"stop_keywords": ["stop"], "start_keywords": ["stop"]}, "cannot both"),
                         ({"send_window": {"start": "9am"}}, "HH:MM"),
                         ({"max_per_customer_per_day": 5, "max_per_customer_per_week": 2}, "more than"),
                         ({"dry_runn": True}, "is not an option")]:
        with pytest.raises(NudgeConfigError, match=message):
            cfg.build_settings(raw)


async def test_a_campaign_starts_paused_and_keeps_only_its_own_trigger_options(session):
    campaign = await store.create_campaign(
        session, {"name": "Cart", "kind": "abandoned_cart"}, actor="test")
    assert campaign.is_active is False
    assert set(campaign.config["trigger"]) == {"after_minutes", "within_hours", "min_value", "min_items"}

    await store.update_campaign(session, campaign,
                                {"config": {"trigger": {"after_minutes": 30}}}, actor="test")
    assert campaign.config["trigger"]["after_minutes"] == 30
    assert campaign.config["trigger"]["within_hours"] == 23     # kept, not reset

    # A new kind starts from that kind's trigger options.
    await store.update_campaign(session, campaign, {"kind": "repeat_purchase",
        "config": {"message": {"text": "Hi {first_name}, order again?"}}}, actor="test")
    assert campaign.config["trigger"] == {"days_since_last_order": 14,
                                          "max_days_since_last_order": None}


async def test_an_unknown_coupon_is_refused(session):
    with pytest.raises(NudgeConfigError, match="no coupon with the code NOPE"):
        await make(session, "targeted_offer", code="nope")


# --- the master switch ----------------------------------------------------------------------
async def test_off_means_nothing_is_sent(session, customer):
    await make(session, "abandoned_cart")
    await left_cart(session, customer)
    await wrote_in(session, customer)
    phone = Phone()

    report = await run(session, phone)            # never switched on: off

    assert report.stopped == "master_switch_off"
    assert phone.sent == [] and await sends(session) == []

    await switch_on(session)
    await store.set_enabled(session, False, actor="test")
    report = await run(session, phone)
    assert report.stopped == "master_switch_off" and phone.sent == []


async def test_a_preview_sends_nothing_even_when_off(session, customer):
    await store.save_settings(session, {"send_window": {"start": "00:00", "end": "23:59"}},
                              actor="test")               # the switch stays off
    campaign = await make(session, "abandoned_cart", active=False)
    await left_cart(session, customer)
    await wrote_in(session, customer)
    phone = Phone()

    report = await run(session, phone, campaign_ids=[campaign.id], preview=True)

    [outcome] = report.outcomes
    assert outcome.status == "would_send" and "2 × Drumstick Sambar" in outcome.message
    assert phone.sent == [] and await sends(session) == []


# --- abandoned cart -------------------------------------------------------------------------
async def test_a_left_cart_gets_one_reminder_with_its_link(session, customer):
    await switch_on(session)
    await make(session, "abandoned_cart")
    await left_cart(session, customer)
    await wrote_in(session, customer)
    phone = Phone()

    report = await run(session, phone)

    [message] = phone.sent
    assert message["channel"] == "button" and message["label"] == "Order now"
    assert message["body"] == ("Hi Asha, your cart is still waiting: 2 × Drumstick Sambar, "
                               "1 × Tomato Rasam. Tap below to finish your order.")
    assert message["url"].startswith("http") and "/order/" in message["url"]
    [row] = await sends(session)
    assert row.status == NudgeStatus.SENT and row.sent_at is not None
    assert report.totals["sent"] == 1

    # The next run sees the same cart and leaves it alone.
    report = await run(session, phone)
    assert len(phone.sent) == 1 and reasons(report)[customer.whatsapp_number] == "already_sent"


async def test_a_cart_too_fresh_or_too_old_is_not_nudged(session, customer):
    await switch_on(session)
    await make(session, "abandoned_cart")
    await wrote_in(session, customer)
    phone = Phone()

    await left_cart(session, customer, minutes_ago=20)      # still browsing
    assert (await run(session, phone)).campaigns[0].candidates == 0
    await left_cart(session, customer, minutes_ago=60 * 30)  # older than within_hours
    assert (await run(session, phone)).campaigns[0].candidates == 0
    assert phone.sent == []


async def test_an_order_placed_after_the_cart_means_it_was_not_abandoned(session, customer):
    await switch_on(session)
    await make(session, "abandoned_cart")
    await left_cart(session, customer, minutes_ago=90)
    await wrote_in(session, customer)
    await order(session, customer, status=PaymentStatus.EXPIRED, days_ago=0,
                stage=OrderStage.PENDING_PAYMENT)

    assert (await run(session)).campaigns[0].candidates == 0


# --- reaching them: the 24-hour window and templates ------------------------------------------
async def test_outside_the_window_without_a_template_is_skipped(session, customer):
    await switch_on(session)
    await make(session, "abandoned_cart")
    await left_cart(session, customer)              # no message from them in 24 hours

    report = await run(session)
    assert reasons(report)[customer.whatsapp_number] == "outside_24h_window"


async def test_outside_the_window_the_template_goes(session):
    customer = Customer(whatsapp_number="919876543210", name="Ravi Kumar")
    session.add(customer)
    await session.flush()
    await switch_on(session)
    await make(session, "abandoned_cart", config={"message": {"template": {
        "name": "cart_reminder", "params": ["{first_name}", "{cart_items}"],
        "button": "order_link"}}})
    await left_cart(session, customer)
    phone = Phone()

    await run(session, phone)

    [message] = phone.sent
    assert message["channel"] == "template" and message["name"] == "cart_reminder"
    assert message["values"] == ("Ravi", "2 × Drumstick Sambar, 1 × Tomato Rasam")
    assert message["button"]                       # the ordering link's token


async def test_marketing_templates_are_not_sent_to_us_numbers(session, customer):
    await switch_on(session)
    await make(session, "abandoned_cart", config={"message": {"template": {"name": "cart_reminder"}}})
    await left_cart(session, customer)              # +1 number, window closed

    report = await run(session)
    assert reasons(report)[customer.whatsapp_number] == "us_marketing_blocked"

    await store.save_settings(session, {"block_marketing_templates_to_us": False}, actor="t")
    phone = Phone()
    await run(session, phone)
    assert phone.sent[0]["channel"] == "template"


async def test_an_unapproved_template_is_not_sent(session, monkeypatch):
    from app.integrations.gallabox import template_status

    async def pending(name):
        return False

    monkeypatch.setattr(template_status, "is_approved", pending)
    customer = Customer(whatsapp_number="919876543210", name="Ravi")
    session.add(customer)
    await session.flush()
    await switch_on(session)
    await make(session, "abandoned_cart", config={"message": {"template": {"name": "cart_reminder"}}})
    await left_cart(session, customer)

    assert reasons(await run(session))["919876543210"] == "template_not_approved"


# --- who is left out ------------------------------------------------------------------------------
async def test_stop_opts_out_from_any_step_and_start_offers_opts_back_in(session, customer):
    phone = Phone()
    with use_sender(phone):
        for step, text in ((ConversationStep.AWAIT_ADDRESS, "STOP"),
                           (ConversationStep.CART_REVIEW, "Start offers")):
            conversation, _ = await cart.for_customer(session, customer)
            conversation.step = step
            ctx = FlowContext(session=session, customer=customer, conversation=conversation,
                              event=InboundEvent(whatsapp_number=customer.whatsapp_number,
                                                 kind=InboundKind.TEXT, text=text))
            assert await _handle_global_intent(ctx)
            if text == "STOP":
                assert customer.nudges_opted_out_at is not None
                assert "won't get offers" in phone.sent[-1]["body"]
    assert customer.nudges_opted_out_at is None and "back on our list" in phone.sent[-1]["body"]


async def test_stop_inside_a_sentence_is_just_a_message(session, customer):
    conversation, _ = await cart.for_customer(session, customer)
    conversation.step = ConversationStep.CART_REVIEW
    ctx = FlowContext(session=session, customer=customer, conversation=conversation,
                      event=InboundEvent(whatsapp_number=customer.whatsapp_number,
                                         kind=InboundKind.TEXT, text="please stop the rasam"))
    with use_sender(Phone()):
        await _handle_global_intent(ctx)
    assert customer.nudges_opted_out_at is None


async def test_opted_out_with_an_agent_or_ordering_are_left_alone(session, customer, outlet):
    await switch_on(session)
    await make(session, "abandoned_cart")
    conversation = await left_cart(session, customer)
    await wrote_in(session, customer)

    customer.nudges_opted_out_at = now()
    assert reasons(await run(session))[customer.whatsapp_number] == "opted_out"
    customer.nudges_opted_out_at = None

    conversation.step = ConversationStep.HANDED_OVER
    assert reasons(await run(session))[customer.whatsapp_number] == "with_an_agent"
    conversation.step = ConversationStep.CART_REVIEW

    await order(session, customer, status=PaymentStatus.LINK_SENT, days_ago=1,
                stage=OrderStage.PENDING_PAYMENT,
                payment_link_expires_at=now() + timedelta(minutes=20))
    assert reasons(await run(session))[customer.whatsapp_number] == "order_in_progress"


async def test_quiet_hours_are_the_customers_local_time(session, customer):
    await switch_on(session)
    local = now().astimezone(__import__("zoneinfo").ZoneInfo("America/New_York"))
    start = (local + timedelta(hours=2)).strftime("%H:%M")
    end = (local + timedelta(hours=3)).strftime("%H:%M")
    await make(session, "abandoned_cart", config={"schedule": {"send_window": {"start": start, "end": end}}})
    await left_cart(session, customer)
    await wrote_in(session, customer)

    assert reasons(await run(session))[customer.whatsapp_number] == "quiet_hours"


async def test_test_numbers_and_audience_filters(session, customer):
    await switch_on(session, test_numbers=["919999999999"])
    await make(session, "abandoned_cart")
    await left_cart(session, customer)
    await wrote_in(session, customer)
    assert reasons(await run(session))[customer.whatsapp_number] == "not_a_test_number"

    await switch_on(session, test_numbers=[])
    campaign = (await store.list_campaigns(session))[0]
    await store.update_campaign(session, campaign, {"config": {"audience": {
        "min_paid_orders": 1}}}, actor="t")
    assert reasons(await run(session))[customer.whatsapp_number] == "audience:min_paid_orders"
    await store.update_campaign(session, campaign, {"config": {"audience": {
        "min_paid_orders": None, "exclude_country_codes": ["1"]}}}, actor="t")
    assert reasons(await run(session))[customer.whatsapp_number] == "audience:exclude_country_codes"


async def test_one_customer_gets_one_nudge_however_many_campaigns_match(session, customer):
    await switch_on(session)
    await make(session, "abandoned_cart", name="First", priority=10)
    await make(session, "never_ordered", name="Second", config={"trigger": {"days_since_signup": 0}})
    await left_cart(session, customer)
    await wrote_in(session, customer)
    phone = Phone()

    report = await run(session, phone)

    assert len(phone.sent) == 1 and "cart is still waiting" in phone.sent[0]["body"]
    assert reasons(report)[customer.whatsapp_number] == "too_soon"


async def test_dry_run_logs_once_and_sends_nothing(session, customer):
    await switch_on(session, dry_run=True)
    await make(session, "abandoned_cart")
    await left_cart(session, customer)
    await wrote_in(session, customer)
    phone = Phone()

    await run(session, phone)
    report = await run(session, phone)

    assert phone.sent == []
    [row] = await sends(session)
    assert row.status == NudgeStatus.DRY_RUN
    assert reasons(report)[customer.whatsapp_number] == "already_logged"

    # Going live sends it for real: a dry-run row is not a send.
    await store.save_settings(session, {"dry_run": False}, actor="t")
    await run(session, phone)
    assert len(phone.sent) == 1


async def test_a_failed_send_is_retried_then_given_up(session, customer):
    await switch_on(session, min_hours_between=0)
    await make(session, "abandoned_cart")
    await left_cart(session, customer)
    await wrote_in(session, customer)
    broken = Phone(fail=True)

    for _ in range(runner.MAX_FAILURES):
        report = await run(session, broken)
        assert report.totals["failed"] == 1
    report = await run(session, broken)
    assert reasons(report)[customer.whatsapp_number] == "gave_up_after_failures"
    rows = await sends(session)
    assert all(r.status == NudgeStatus.FAILED and r.claim_key is None for r in rows)
    assert "channel down" in rows[0].error


# --- the other kinds ------------------------------------------------------------------------------
async def test_an_unpaid_order_is_chased_once_until_they_order_again(session, customer):
    await switch_on(session)
    await make(session, "abandoned_checkout")
    await wrote_in(session, customer)
    unpaid = await order(session, customer, status=PaymentStatus.EXPIRED, days_ago=0,
                         stage=OrderStage.PENDING_PAYMENT,
                         created_at=now() - timedelta(hours=5),
                         payment_link_expires_at=now() - timedelta(hours=4))
    phone = Phone()

    await run(session, phone)
    assert unpaid.order_number in phone.sent[0]["body"] and "Veg Biryani" in phone.sent[0]["body"]
    # The link opens with the unpaid order's dishes back in the cart.
    conversation, lines = await cart.for_customer(session, customer)
    assert [(line["retailer_id"], line["quantity"]) for line in lines] == [("biryani", 1)]

    await run(session, phone)
    assert len(phone.sent) == 1

    # Hours later that refilled cart is not chased again as an abandoned cart...
    await make(session, "abandoned_cart")
    earlier = (now() - timedelta(hours=2)).isoformat()
    conversation.set(cart_updated_at=earlier, cart_restored_at=earlier)
    conversation.last_message_at = now() - timedelta(hours=2)
    await session.flush()
    report = await run(session, phone, preview=True)
    assert report.campaigns[-1].candidates == 0
    # ...until they change it themselves.
    cart.set_quantity(conversation, retailer_id="biryani", name="Veg Biryani", price="9", quantity=2)
    conversation.set(cart_updated_at=(now() - timedelta(minutes=90)).isoformat())
    await session.flush()
    report = await run(session, phone, preview=True)
    assert report.campaigns[-1].candidates == 1


async def test_a_retried_order_is_not_chased(session, customer):
    await switch_on(session)
    await make(session, "abandoned_checkout")
    await order(session, customer, status=PaymentStatus.EXPIRED, days_ago=0,
                stage=OrderStage.PENDING_PAYMENT,
                payment_link_expires_at=now() - timedelta(hours=4),
                created_at=now() - timedelta(hours=5))
    await order(session, customer, days_ago=0)        # paid since
    report = await run(session)
    assert report.campaigns[0].candidates == 0


async def test_repeat_purchase_reminds_once_per_last_order(session, customer):
    await switch_on(session)
    await make(session, "repeat_purchase")
    await wrote_in(session, customer)
    await order(session, customer, days_ago=20)
    phone = Phone()

    await run(session, phone)
    assert "20 days since your last Shero Home Food meal" in phone.sent[0]["body"]
    assert "1 × Veg Biryani" in phone.sent[0]["body"]
    [row] = await sends(session)
    assert row.dedupe_key.startswith("after:")

    await run(session, phone)
    assert len(phone.sent) == 1


async def test_never_ordered_reaches_new_customers_only(session, customer):
    await switch_on(session)
    await make(session, "never_ordered")
    customer.created_at = now() - timedelta(days=2)
    await wrote_in(session, customer)
    regular = Customer(whatsapp_number="17325550199", name="Regular",
                       created_at=now() - timedelta(days=2))
    session.add(regular)
    await session.flush()
    await order(session, regular, days_ago=1)
    phone = Phone()

    report = await run(session, phone)
    assert report.campaigns[0].candidates == 1 and phone.sent[0]["to"] == customer.whatsapp_number


async def test_a_targeted_offer_carries_its_coupon_and_applies_it(session, customer):
    promo = await coupon(session, ends_on=(now() + timedelta(days=10)).date())
    await switch_on(session)
    await make(session, "targeted_offer", code="comeback")
    await wrote_in(session, customer)
    phone = Phone()

    await run(session, phone)

    body = phone.sent[0]["body"]
    assert "20% off with code COMEBACK" in body and "Valid till" in body
    conversation, _ = await cart.for_customer(session, customer)
    assert promotions.stored_code(conversation) == "COMEBACK"    # applied on the ordering page
    [row] = await sends(session)
    assert row.promo_code == promo.code and row.dedupe_key == "offer#1"


async def test_customers_who_cannot_use_the_coupon_are_skipped(session, customer):
    await coupon(session, first_order_only=True)
    await switch_on(session)
    await make(session, "targeted_offer", code="COMEBACK")
    await wrote_in(session, customer)
    await order(session, customer, days_ago=5)

    report = await run(session)
    assert reasons(report)[customer.whatsapp_number].startswith("coupon: COMEBACK is for your first order")


async def test_a_deleted_coupon_pauses_the_campaign_with_a_reason(session, customer):
    promo = await coupon(session)
    await switch_on(session)
    await make(session, "targeted_offer", code="COMEBACK")
    await session.delete(promo)
    await session.flush()

    report = await run(session)
    assert report.campaigns[0].status == "invalid" and "needs a coupon" in report.campaigns[0].reason


# --- Zoho and stats -------------------------------------------------------------------------------
async def test_each_send_is_noted_and_tagged_in_zoho(session, customer, monkeypatch):
    from app.integrations.zoho import engagement
    calls = []

    async def note(module, record_id, *, title, content):
        calls.append(("note", module, record_id, title, content))

    async def tags(module, record_id, names):
        calls.append(("tags", module, record_id, names))

    monkeypatch.setattr(engagement, "add_note", note)
    monkeypatch.setattr(engagement, "add_tags", tags)
    customer.zoho_lead_id = "L-1"
    await switch_on(session)
    await make(session, "abandoned_cart")
    await left_cart(session, customer)
    await wrote_in(session, customer)

    await run(session)

    assert calls[0][:4] == ("note", "Leads", "L-1", "WhatsApp nudge: abandoned_cart test")
    assert "your cart is still waiting" in calls[0][4]
    assert calls[1] == ("tags", "Leads", "L-1", ["Nudge: Abandoned cart"])
    [row] = await sends(session)
    assert row.zoho_synced is True


async def test_zoho_being_down_does_not_unsend_a_nudge(session, customer, monkeypatch):
    from app.integrations.zoho import engagement

    async def down(*args, **kwargs):
        raise IntegrationError("zoho", "502")

    monkeypatch.setattr(engagement, "add_note", down)
    customer.zoho_contact_id = "C-1"
    await switch_on(session)
    await make(session, "abandoned_cart")
    await left_cart(session, customer)
    await wrote_in(session, customer)

    report = await run(session)
    [row] = await sends(session)
    assert report.totals["sent"] == 1 and row.status == NudgeStatus.SENT
    assert row.zoho_synced is False and "502" in row.zoho_error


async def test_stats_credit_the_order_that_followed(session, customer):
    await switch_on(session)
    campaign = await make(session, "abandoned_cart")
    await left_cart(session, customer)
    await wrote_in(session, customer)
    await run(session)
    await order(session, customer, days_ago=0, total="42.50")

    result = await reports.stats(session, days=7)

    [entry] = result["campaigns"]
    assert entry["campaign_id"] == str(campaign.id)
    assert (entry["sent"], entry["customers"], entry["conversions"], entry["revenue"]) == (1, 1, 1, "42.50")
    assert result["totals"]["conversion_rate"] == 1.0


# --- a test send ----------------------------------------------------------------------------------
async def test_a_test_send_uses_sample_data_for_a_stranger(session):
    await store.save_settings(session, {}, actor="t")             # nudges off
    campaign = await make(session, "abandoned_cart", active=False)
    # Not a customer, but they have messaged the bot: the window is open.
    session.add(InboundMessage(provider_message_id="m-1", whatsapp_number="+919800000001",
                               payload={}, created_at=now() - timedelta(hours=1)))
    await session.flush()
    phone = Phone()

    with use_sender(phone):
        result = await runner.send_test(session, campaign, "+91 98000 00001", actor="t")

    assert result["sent"] and result["used_sample_data"]
    assert "2 × Drumstick Sambar" in phone.sent[0]["body"]


async def test_a_test_send_explains_a_closed_window(session, customer):
    campaign = await make(session, "repeat_purchase", active=False)
    result = await runner.send_test(session, campaign, customer.whatsapp_number, actor="t")
    assert (result["sent"], result["reason"]) == (False, "outside_24h_window")
    assert "24 hours" in result["hint"]
