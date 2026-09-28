"""Zoho sync on Shero's CRM: food customers are Leads with a Bot Stage.

Contacts there are Kitchen Partners, so a Lead is never converted; paid
orders go in the Orders module, linked to the Lead.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.db.models import LeadStage, Order, OrderStage, PaymentStatus
from app.integrations.zoho import crm
from app.integrations.zoho import fields as f
from app.services import crm_sync

pytestmark = pytest.mark.asyncio


@pytest.fixture
def zoho(monkeypatch):
    """Records every Zoho write instead of making it."""
    class Calls(list):
        state: dict = {}

    calls = Calls()
    calls.state = {"existing": None}

    async def find(_phone):
        return calls.state["existing"]

    async def create_lead(fields):
        calls.append(("create_lead", fields))
        return "lead-1"

    async def update_lead(zoho_id, fields):
        calls.append(("update_lead", zoho_id, fields))

    async def create_order(fields):
        calls.append(("create_order", fields))
        return "zorder-1"

    async def update_order_stage(zoho_id, stage, *, at=None):
        calls.append(("order_stage", zoho_id, stage))

    for name, fn in {"find_lead_by_phone": find, "create_lead": create_lead,
                     "update_lead": update_lead, "create_order": create_order,
                     "update_order_stage": update_order_stage}.items():
        monkeypatch.setattr(crm_sync.crm, name, fn)
    return calls


def _writes(calls, kind):
    return [c for c in calls if c[0] == kind]


# --- the Lead ----------------------------------------------------------------
async def test_a_new_number_becomes_a_lead(customer, zoho):
    customer.name = "Nagi Pragalathan"
    customer.ad_id, customer.campaign_id = "ad-9", "camp-3"
    assert await crm_sync.ensure_lead(customer) == "lead-1"

    (_, fields), = _writes(zoho, "create_lead")
    assert fields[f.L_FIRST_NAME] == "Nagi" and fields[f.L_LAST_NAME] == "Pragalathan"
    assert fields[f.L_PHONE] == "+" + customer.whatsapp_number.lstrip("+")
    assert fields[f.L_LEAD_SOURCE] == "Whatsapp"
    assert fields[f.L_AD_ID] == "ad-9" and fields[f.L_CAMPAIGN_ID] == "camp-3"
    assert fields[f.L_BOT_STAGE] == customer.lead_stage
    assert customer.zoho_lead_id == "lead-1"


async def test_a_number_already_in_zoho_is_linked_not_duplicated(customer, zoho):
    zoho.state["existing"] = {"id": "lead-old"}
    assert await crm_sync.ensure_lead(customer) == "lead-old"
    assert _writes(zoho, "create_lead") == []


async def test_a_stage_change_updates_bot_stage_and_its_history(customer, zoho):
    customer.zoho_lead_id = "lead-1"
    customer.lead_stage = str(LeadStage.CART_CREATED)
    customer.stage_timestamps = {"Cart Created": "2026-01-05T10:00:00+00:00"}

    await crm_sync.advance_stage(customer, LeadStage.OUTLET_SELECTED, forward_only=True)

    (_, zoho_id, fields), = _writes(zoho, "update_lead")
    assert zoho_id == "lead-1"
    assert fields[f.L_BOT_STAGE] == "Outlet Selected"
    history = fields[f.L_BOT_STAGE_HISTORY].splitlines()
    assert history[0] == "2026-01-05 10:00 UTC  Cart Created"
    assert history[-1].endswith("Outlet Selected")


async def test_the_address_lands_on_the_leads_own_fields(customer, zoho):
    customer.zoho_lead_id = "lead-1"
    await crm_sync.push_details(customer, address="12 Maple St", apartment_unit="2B",
                                postal_code="08820", latitude=40.5, longitude=-74.41,
                                outlet_name="Shero Edison", distance_km=3.2,
                                unknown="ignored")

    (_, _, fields), = _writes(zoho, "update_lead")
    assert fields[f.L_STREET] == "12 Maple St"
    assert fields[f.L_ADDRESS_LINE_2] == "2B"
    assert fields[f.L_ZIP] == "08820"
    assert fields[f.L_LATITUDE] == "40.500000" and fields[f.L_LONGITUDE] == "-74.410000"
    assert fields[f.L_SELECTED_OUTLET] == "Shero Edison"
    assert fields[f.L_DISTANCE_KM] == 3.2
    assert "unknown" not in fields


@pytest.mark.parametrize("cuisine,option", [
    ("kerala", "Kerala"), ("Chettinad", "Chettinad"), ("andhra", "Andhra"),
    ("mexican", None), (None, None),
])
async def test_the_cuisine_is_written_only_as_an_existing_picklist_option(
        customer, zoho, cuisine, option):
    customer.zoho_lead_id = "lead-1"
    await crm_sync.push_details(customer, cuisine=cuisine)
    written = [c[2] for c in _writes(zoho, "update_lead")]
    assert (written[0].get(f.L_CUISINE) if written else None) == option


async def test_zoho_being_down_never_breaks_the_conversation(customer, monkeypatch):
    from app.core.exceptions import IntegrationError

    async def down(_phone):
        raise IntegrationError("zoho", "503")
    monkeypatch.setattr(crm_sync.crm, "find_lead_by_phone", down)

    assert await crm_sync.advance_stage(customer, LeadStage.CART_CREATED) is True
    assert customer.lead_stage == "Cart Created"     # still tracked locally


# --- funnel order ------------------------------------------------------------
@pytest.mark.parametrize("current,stage,backwards", [
    (LeadStage.SLOT_SELECTED, LeadStage.CART_CREATED, True),
    (LeadStage.CART_CREATED, LeadStage.SLOT_SELECTED, False),
    (LeadStage.PAYMENT_LINK_SENT, LeadStage.OUTLET_SELECTED, True),
    # Outside the funnel ends an attempt: the next may start anywhere.
    (LeadStage.CONVERTED, LeadStage.CART_CREATED, False),
    (LeadStage.PAYMENT_ABANDONED, LeadStage.CART_CREATED, False),
    (LeadStage.NOT_SERVICEABLE, LeadStage.OUTLET_SELECTED, False),
])
async def test_backwards_is_judged_by_funnel_order(current, stage, backwards):
    assert crm_sync.is_backwards(str(current), stage) is backwards


async def test_editing_the_cart_after_picking_a_slot_does_not_rewind(customer, zoho):
    customer.lead_stage = str(LeadStage.SLOT_SELECTED)
    assert await crm_sync.advance_stage(customer, LeadStage.CART_CREATED,
                                        forward_only=True) is False
    assert zoho == []


# --- payment: converted, and the Order ---------------------------------------
def _paid_order(customer) -> Order:
    return Order(order_number="SHO-TEST-9", customer_id=customer.id,
                 items=[{"name": "Sambar", "quantity": 2, "unit_price": "9.50"}],
                 item_count=2, dish_total=Decimal("19.00"), delivery_fee=Decimal("4.00"),
                 extra_fees=Decimal("1.00"), tax=Decimal("1.50"), total=Decimal("25.50"),
                 delivery_address="12 Maple St", apartment_unit="2B", postal_code="08820",
                 contact_number="917401268091", slot_label="Fri 7:00 PM - 8:00 PM",
                 stripe_payment_intent_id="pi_1",
                 created_at=datetime(2026, 9, 28, 10, 0, 5, 123, tzinfo=timezone.utc),
                 paid_at=datetime(2026, 9, 28, 10, 5, tzinfo=timezone.utc),
                 payment_status=PaymentStatus.PAID, stage=OrderStage.PAID_SLOT_BOOKED)


async def test_payment_marks_the_lead_converted_and_files_the_order(
        session, customer, zoho):
    customer.zoho_lead_id = "lead-1"
    customer.cuisine_preference = "Kerala"
    order = _paid_order(customer)
    session.add(order)
    await session.flush()

    await crm_sync.convert_and_record_order(customer, order, "Shero Edison")

    (_, _, fields), = _writes(zoho, "update_lead")
    assert fields[f.L_BOT_STAGE] == "Converted"
    assert customer.is_converted

    (_, o), = _writes(zoho, "create_order")
    assert o[f.O_ORDER_NO] == "SHO-TEST-9"
    assert o[f.O_LEAD] == {"id": "lead-1"}
    assert o[f.O_CHANNEL] == "WhatsApp Bot"
    assert o[f.O_STATUS] == "Confirmed"
    assert o[f.O_ITEMS] == [{f.I_NAME: "Sambar", f.I_QUANTITY: 2, f.I_UNIT_PRICE: 9.5}]
    assert o[f.O_ORDER_TOTAL] == 25.5 and o[f.O_DELIVERY_CHARGE] == 4.0
    assert o[f.O_TAXES_AND_FEES] == 2.5
    assert o[f.O_ADDRESS] == "12 Maple St, 2B, 08820"
    assert o[f.O_CUSTOMER_NO] == "+917401268091"
    assert o[f.O_PLACED_TIME] == "2026-09-28T10:00:05+00:00"     # no microseconds
    assert o[f.O_STRIPE_PAYMENT_ID] == "pi_1"
    assert order.zoho_order_id == "zorder-1"

    # A replayed payment must not file it twice.
    await crm_sync.convert_and_record_order(customer, order, "Shero Edison")
    assert len(_writes(zoho, "create_order")) == 1


@pytest.mark.parametrize("stage,status", [
    (OrderStage.PENDING_PAYMENT, "Placed"),
    (OrderStage.PAID_SLOT_BOOKED, "Confirmed"),
    (OrderStage.SENT_TO_KITCHEN, "Confirmed"),
    (OrderStage.OUT_FOR_DELIVERY, "Dispatched"),
    (OrderStage.DELIVERED, "Completed"),
    (OrderStage.CANCELLED, "Cancelled"),
    (OrderStage.REFUNDED, "Cancelled"),
])
async def test_every_order_stage_maps_to_an_existing_order_status(stage, status):
    assert crm.ORDER_STATUS[stage] == status


async def test_delivery_updates_move_the_zoho_order(customer, zoho):
    order = _paid_order(customer)
    order.zoho_order_id = "zorder-1"
    await crm_sync.push_order_stage(order, OrderStage.OUT_FOR_DELIVERY)
    assert _writes(zoho, "order_stage") == [("order_stage", "zorder-1",
                                             OrderStage.OUT_FOR_DELIVERY)]


async def test_a_lead_is_never_converted_and_contacts_are_never_written():
    import inspect

    source = inspect.getsource(crm) + inspect.getsource(crm_sync)
    for forbidden in ('"Contacts"', "convert_lead", "actions/convert"):
        assert forbidden not in source


async def test_the_phone_search_matches_with_or_without_the_plus(monkeypatch):
    seen = {}

    async def search(module, criteria):
        seen.update(module=module, criteria=criteria)
        return []
    monkeypatch.setattr(crm.zoho_client, "search", search)

    assert await crm.find_lead_by_phone("917401268091") is None
    assert seen["module"] == "Leads"
    assert seen["criteria"] == ("((Phone:equals:+917401268091)"
                                "or(Phone:equals:917401268091))")
