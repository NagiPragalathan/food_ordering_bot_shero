"""DST India's nudges and CRM: its own message names, its F1-F12 templates
usable as nudges, nudges that run without Zoho, Zoho skipped quietly, and the
DST CRM's stages (services/dst_crm.py). Shero US is unchanged."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core import tenants
from app.core.config import settings_for
from app.db.models import LeadStage, Order, OrderStage
from app.integrations.gallabox import template_admin
from app.services import crm_sync, dst_crm
from app.services.nudges import config, crm, messages, runner
from app.services.nudges.config import Message, NudgeSettings
from tests.services.test_nudges import Phone, at_stage, phone  # noqa: F401 - fixture

CART = LeadStage.CART_CREATED.value
# 11:30 in Kolkata: inside DST India's 9:00-21:00 send hours.
NOW = datetime.now(timezone.utc).replace(hour=6, minute=0, second=0, microsecond=0)


@pytest.fixture
def dst():
    with tenants.use(tenants.DST):
        yield settings_for(tenants.DST)


# --- message names and what can be added ---------------------------------------------
def test_each_business_names_its_own_messages(dst):
    assert messages.prefix() == "dst_nudge_"
    assert messages.name_for("Cart reminder", set()) == "dst_nudge_cart_reminder"
    assert messages.name_for("Cart reminder", {"dst_nudge_cart_reminder"}) == \
        "dst_nudge_cart_reminder_2"


def test_shero_us_names_are_unchanged():
    assert messages.prefix() == "shero_nudge_" == messages.PREFIX
    assert messages.name_for("Cart reminder", set()) == "shero_nudge_cart_reminder"
    assert messages.addable_prefixes() == ("smo_", "shero_nudge_")


def _row(name, text, category="MARKETING", buttons=()):
    components = [{"type": "BODY", "text": text}]
    if buttons:
        components.append({"type": "BUTTONS", "buttons": [
            {"type": "QUICK_REPLY", "text": b} for b in buttons]})
    return {"name": name, "status": "APPROVED", "category": category, "components": components}


@pytest.fixture
def gallabox_rows(monkeypatch):
    rows: list[dict] = []

    async def listing():
        return rows

    monkeypatch.setattr(template_admin, "list_templates", listing)
    messages.clear_cache()
    yield rows
    messages.clear_cache()


async def test_dst_s_follow_up_templates_can_be_added(dst, gallabox_rows):
    gallabox_rows += [
        _row("dst_rnr_1", "Hi {{1}}, just checking in — are you still planning your "
                          "order with us?"),
        _row("dst_win_back", "Hi {{1}}, it's been a while! Here's 10% off your next order "
                             "with Shero Home Food."),
        # Needs the quotation link: not a nudge.
        _row("dst_quotation_party", "Hi {{1}}, your quotation is ready: {{2}}", "UTILITY"),
        # Shero US's own, and the ordering catalogue's DST copy: not DST's nudges.
        _row("smo_hello", "Order your Shero meal today."),
        _row("dst_shero_feedback", "How was your Shero meal today? Rate order #{{1}} below.",
             buttons=("Great", "Good", "Poor")),
        _row("dst_nudge_cart", "Your cart is waiting. Order today?",
             buttons=("Order Food", "Later", "No")),
    ]
    found = {m["name"]: m for m in await messages.addable(set())}
    assert sorted(found) == ["dst_nudge_cart", "dst_rnr_1", "dst_win_back"]
    assert found["dst_rnr_1"]["text"].startswith("Hi {name}, just checking in")
    # Kept as a message, it sends the first name as {{1}}, as approved.
    spec = messages.spec(Message(**found["dst_rnr_1"]))
    assert spec.params == ("customer_name",) and spec.sample_body.startswith("Hi {{1}},")


async def test_shero_us_list_is_unchanged(gallabox_rows):
    gallabox_rows += [_row("smo_hello", "Order your Shero meal today."),
                      _row("smo_named", "Hi {{1}}, your Shero meal is waiting."),
                      _row("dst_rnr_1", "Hi {{1}}, just checking in.")]
    assert [m["name"] for m in await messages.addable(set())] == ["smo_hello"]


# --- the settings' defaults ------------------------------------------------------------
def test_dst_nudges_default_to_kolkata_and_no_zoho(dst):
    cfg = NudgeSettings()
    assert (cfg.timezone, cfg.zoho_sync) == ("Asia/Kolkata", False)
    assert crm.in_use() is False


def test_us_nudges_keep_their_defaults():
    cfg = NudgeSettings()
    assert (cfg.timezone, cfg.zoho_sync) == ("America/New_York", True)
    assert crm.in_use() is True


def test_zoho_counts_for_dst_once_connected(dst, monkeypatch):
    for key in ("zoho_client_id", "zoho_client_secret", "zoho_refresh_token"):
        monkeypatch.setattr(dst, key, "x")
    assert crm.in_use() is True


# --- a DST run, on the bot's own stages --------------------------------------------------
@pytest.fixture
def no_zoho(monkeypatch):
    """Any Zoho call fails the test."""
    def refuse(*_, **__):
        raise AssertionError("Zoho was called for a business without Zoho")

    for name in ("ensure", "follow", "sync", "file_message"):
        monkeypatch.setattr(crm, name, refuse)


async def test_dst_nudges_run_without_zoho(session, customer, phone, dst, no_zoho):  # noqa: F811
    text = "Hi {name}, your Shero meal is still in your cart. Would you like it today?"
    data = NudgeSettings(messages=[Message(name="dst_nudge_cart", text=text)]).model_dump()
    data["enabled"] = True
    # Even with the box ticked, nothing goes to Zoho while it is not connected.
    data["zoho_sync"] = True
    for rule in data["rules"]:
        if rule["stage"] == CART:
            rule.update(active=True, message="dst_nudge_cart", wait_hours=3,
                        no_response_hours=24)
    await config.save(session, data, "test")
    at_stage(customer, CART, hours_ago=4, now=NOW)

    counts = await runner.run(session, NOW)
    assert counts["sent"] == 1 and phone.nudges() == ["dst_nudge_cart"]
    assert phone.sent[-1].payload["values"] == ["Asha"]

    # No answer for a day: into No response, still without Zoho.
    await runner.run(session, NOW + timedelta(hours=25))
    from tests.services.test_nudges import lead_of
    assert (await lead_of(session, customer)).bucket == "no_response"


# --- crm_sync without Zoho ---------------------------------------------------------------
@pytest.fixture
def zoho_untouched(monkeypatch):
    """Every Zoho call fails the test, and every error log is kept."""
    from app.integrations.zoho import crm as zoho_crm

    def refuse(*_, **__):
        raise AssertionError("Zoho was called")

    for name in ("find_by_phone", "create_lead", "update_record", "convert_lead",
                 "create_order", "get_record", "update_order_stage"):
        monkeypatch.setattr(zoho_crm, name, refuse)
    errors = []
    monkeypatch.setattr(crm_sync.log, "error", lambda event, **kw: errors.append(event))
    return errors


@pytest.fixture
def dst_crm_on(monkeypatch):
    """DST India's CRM connected (its address and key set)."""
    own = settings_for(tenants.DST)
    monkeypatch.setattr(own, "crm_api_url", "https://crm.example.test")
    monkeypatch.setattr(own, "crm_api_token", "test-token")


async def queued(session) -> list[tuple[str, str | None]]:
    """What is waiting for the DST CRM: (event, pipeline stage), in order."""
    from sqlalchemy import select

    from app.db.models import CrmOutbox

    rows = (await session.execute(select(CrmOutbox).order_by(CrmOutbox.queued_at))).scalars()
    return [(r.event, (r.payload.get("stage") or {}).get("pipeline")) for r in rows]


async def test_dst_stages_go_to_its_crm_and_nothing_to_zoho(session, customer, dst,
                                                          zoho_untouched, dst_crm_on):
    assert await crm_sync.advance_stage(customer, LeadStage.CART_CREATED) is True
    assert customer.lead_stage == LeadStage.CART_CREATED and customer.zoho_lead_id is None
    await crm_sync.push_details(customer, name="Asha")

    order = Order(order_number="DST-261116-AB12X", customer_id=customer.id, items=[])
    session.add(order)
    await session.flush()
    await crm_sync.convert_and_record_order(customer, order, crm_sync.OrderContext())
    await crm_sync.push_order_stage(order, OrderStage.OUT_FOR_DELIVERY)

    assert customer.lead_stage == LeadStage.CONVERTED and customer.is_converted
    assert order.zoho_order_id is None and order.zoho_note is None
    assert await queued(session) == [("lead.stage", "requirementCapture"),
                                     ("order.paid", "orderConfirmed"),
                                     ("order.status", "deliveryStatus")]
    assert zoho_untouched == []          # quiet: no "zoho_..._failed" errors


async def test_push_to_zoho_says_it_is_not_connected(session, customer, dst, zoho_untouched):
    problems = await crm_sync.push_customer(customer, details={}, orders=[])
    assert problems == ["Zoho is not connected for DST India: connect it on the Settings "
                        "page first."]


async def test_us_still_goes_to_zoho_and_not_to_the_dst_crm(session, customer, monkeypatch,
                                                           dst_crm_on):
    from app.integrations.zoho import crm as zoho_crm

    async def none(module, number):
        return None

    async def create(fields):
        return "L1"

    async def update(*_, **__):
        return None

    monkeypatch.setattr(zoho_crm, "find_by_phone", none)
    monkeypatch.setattr(zoho_crm, "create_lead", create)
    monkeypatch.setattr(zoho_crm, "update_record", update)
    assert await crm_sync.advance_stage(customer, LeadStage.CART_CREATED) is True
    assert customer.zoho_lead_id == "L1" and await queued(session) == []


# --- the DST CRM's stages ------------------------------------------------------------------
def test_the_funnel_maps_onto_the_dst_crm():
    assert dst_crm.stage_for(LeadStage.NEW_ENQUIRY) == "welcome"
    for stage in (LeadStage.DETAILS_CAPTURED, LeadStage.CART_CREATED, LeadStage.SLOT_SELECTED,
                  LeadStage.PAYMENT_LINK_SENT, LeadStage.PAYMENT_ABANDONED):
        assert dst_crm.stage_for(stage) == "requirementCapture"
    assert dst_crm.stage_for(LeadStage.CONVERTED) == "orderConfirmed"
    assert dst_crm.stage_for(LeadStage.NOT_SERVICEABLE) is None
    assert dst_crm.stage_for_order(OrderStage.OUT_FOR_DELIVERY) == "deliveryStatus"
    assert dst_crm.stage_for_order(OrderStage.CANCELLED) is None
    assert set(dst_crm.FOR_LEAD_STAGE.values()) | set(dst_crm.FOR_ORDER_STAGE.values()) \
        <= set(dst_crm.STAGES)
    # The 13 stages exactly as the CRM keys them (its pipeline_stages table).
    assert list(dst_crm.STAGES) == [
        "welcome", "requirementCapture", "rnr1", "rnr2", "rnrFinal", "quotationSent",
        "quotationFollowup", "quotationExpiring", "revisedQuotation", "orderConfirmed",
        "deliveryStatus", "lostFeedback", "winback"]


async def test_nothing_is_queued_until_the_dst_crm_is_connected(session, customer, dst):
    assert await dst_crm.record_lead_stage(customer, LeadStage.NEW_ENQUIRY) == "welcome"
    assert await queued(session) == []


async def test_the_dst_crm_is_dst_india_s_only(session, customer, dst_crm_on):
    assert dst_crm.in_use() is False
    assert await dst_crm.record_lead_stage(customer, LeadStage.NEW_ENQUIRY) is None
    assert await queued(session) == []
