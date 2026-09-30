"""Admin Customers page: delete test data, push to Zoho."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.exceptions import IntegrationError
from app.db.models import (
    Conversation,
    Customer,
    CustomerAddress,
    DeliverySlot,
    Order,
    OrderStage,
    PaymentStatus,
    SlotHold,
    SlotHoldStatus,
)
from app.services import crm_sync, customer_admin
from app.services import customers as customer_service

pytestmark = pytest.mark.asyncio


async def _order(session, customer, outlet, number, *, paid, hold_status) -> Order:
    start = datetime.now(timezone.utc) + timedelta(hours=3)
    slot = DeliverySlot(outlet_id=outlet.id, starts_at=start,
                        ends_at=start + timedelta(hours=1), capacity=2, reserved_count=1)
    session.add(slot)
    await session.flush()
    order = Order(order_number=number, customer_id=customer.id, outlet_id=outlet.id,
                  slot_id=slot.id, items=[], dish_total=Decimal("10.00"),
                  total=Decimal("12.00"),
                  payment_status=PaymentStatus.PAID if paid else PaymentStatus.LINK_SENT,
                  stage=OrderStage.PAID_SLOT_BOOKED if paid else OrderStage.PENDING_PAYMENT)
    session.add(order)
    await session.flush()
    session.add(SlotHold(slot_id=slot.id, order_id=order.id, status=hold_status))
    await session.flush()
    return order


async def _count(session, model) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)))


async def test_delete_removes_everything_and_frees_the_slots(session, customer, outlet):
    await customer_service.get_or_create_conversation(session, customer)
    session.add(CustomerAddress(customer_id=customer.id, label="Home",
                                address_line1="1 Oak St", postal_code="08820",
                                is_default=True))
    await _order(session, customer, outlet, "SHO-TEST-1", paid=True,
                 hold_status=SlotHoldStatus.BOOKED)
    await _order(session, customer, outlet, "SHO-TEST-2", paid=False,
                 hold_status=SlotHoldStatus.HELD)

    removed = await customer_admin.delete_customer(session, customer)

    assert removed == {"orders": 2, "addresses": 1}
    for model in (Customer, Conversation, CustomerAddress, Order, SlotHold):
        assert await _count(session, model) == 0, model.__name__
    slots = list((await session.execute(select(DeliverySlot))).scalars())
    assert [s.reserved_count for s in slots] == [0, 0]


async def test_the_list_counts_orders_and_flags_what_is_missing_in_zoho(
        session, customer, outlet):
    await _order(session, customer, outlet, "SHO-TEST-3", paid=True,
                 hold_status=SlotHoldStatus.BOOKED)
    customer.zoho_lead_id = "L1"
    await session.flush()

    [row] = await customer_admin.list_customers(session)
    assert (row.orders, row.paid_orders, row.unsynced_orders) == (1, 1, 1)
    assert not row.in_zoho          # the paid order is not in Zoho yet


@pytest.fixture
def zoho(monkeypatch):
    """A fake Zoho: records what was sent."""
    state = {"leads": {"GONE"}, "created": [], "updated": [], "orders": []}

    async def get_record(_module, zoho_id):
        return None if zoho_id == "GONE" else {"id": zoho_id}

    async def find_by_phone(_module, _number):
        return None

    async def create_lead(fields):
        state["created"].append(fields)
        return "NEW"

    async def update_record(_module, zoho_id, fields):
        state["updated"].append((zoho_id, fields))

    async def create_order(fields):
        state["orders"].append(fields)
        return "O1"

    async def update_order(zoho_id, fields):
        state.setdefault("order_updates", []).append((zoho_id, fields))

    async def create_order_item(fields):
        state.setdefault("items", []).append(fields)
        return "I1"

    async def update_order_item(zoho_id, fields):
        state.setdefault("item_updates", []).append((zoho_id, fields))

    async def find_nothing(*_args):
        return None

    async def create_vendor(fields):
        state.setdefault("vendors", []).append(fields)
        return "V1"

    async def create_product(fields):
        state.setdefault("products", []).append(fields)
        return "P1"

    async def update_quietly(_zoho_id, _fields):
        return None

    for name, fn in [("get_record", get_record), ("find_by_phone", find_by_phone),
                     ("create_lead", create_lead), ("update_record", update_record),
                     ("create_order", create_order), ("update_order", update_order),
                     ("create_order_item", create_order_item),
                     ("update_order_item", update_order_item),
                     ("find_vendor", find_nothing), ("create_vendor", create_vendor),
                     ("update_vendor", update_quietly),
                     ("find_product_by_code", find_nothing),
                     ("create_product", create_product),
                     ("update_product", update_quietly),
                     ("create_sales_order", create_product),
                     ("update_sales_order", update_quietly)]:
        monkeypatch.setattr(crm_sync.crm, name, fn)
    return state


async def test_push_recreates_a_lead_deleted_in_zoho_and_files_paid_orders(
        session, customer, outlet, zoho):
    customer.zoho_lead_id = "GONE"
    session.add(CustomerAddress(customer_id=customer.id, label="Home",
                                address_line1="1 Oak St", postal_code="08820",
                                is_default=True))
    paid = await _order(session, customer, outlet, "SHO-TEST-4", paid=True,
                        hold_status=SlotHoldStatus.BOOKED)
    await _order(session, customer, outlet, "SHO-TEST-5", paid=False,
                 hold_status=SlotHoldStatus.HELD)

    assert await customer_admin.push_to_zoho(session, customer) == []

    assert customer.zoho_lead_id == "NEW" and len(zoho["created"]) == 1
    [(lead_id, fields)] = zoho["updated"]
    assert lead_id == "NEW" and fields["Street"] == "1 Oak St"
    assert [o["Name"] for o in zoho["orders"]] == ["SHO-TEST-4"]   # unpaid skipped
    assert paid.zoho_order_id == "O1"
    # The kitchen went along as a Vendor, and the order points at it.
    assert [v["Vendor_Name"] for v in zoho["vendors"]] == ["Shero Edison"]
    assert zoho["orders"][0]["Outlet"] == {"id": "V1"} and outlet.zoho_vendor_id == "V1"

    # Pushing again does not file the order twice; it refreshes its links.
    assert await customer_admin.push_to_zoho(session, customer) == []
    assert len(zoho["orders"]) == 1
    assert [zoho_id for zoho_id, _ in zoho["order_updates"]] == ["O1"]


async def test_push_reports_a_zoho_failure_instead_of_hiding_it(
        session, customer, monkeypatch, zoho):
    async def broken(_module, _zoho_id, _fields):
        raise IntegrationError("zoho", "INVALID_DATA")
    monkeypatch.setattr(crm_sync.crm, "update_record", broken)

    problems = await customer_admin.push_to_zoho(session, customer)
    assert problems and "INVALID_DATA" in problems[0]


# --- the Zoho column tells the truth --------------------------------------------
@pytest.fixture
def zoho_online(monkeypatch):
    monkeypatch.setattr(customer_admin, "zoho_connected", lambda: True)


async def test_a_link_zoho_no_longer_has_is_dropped_and_shown_as_deleted(
        session, customer, outlet, monkeypatch, zoho_online):
    customer.zoho_lead_id = "L-gone"
    order = await _order(session, customer, outlet, "SHO-TEST-6", paid=True,
                         hold_status=SlotHoldStatus.BOOKED)
    order.zoho_order_id = "O-gone"
    await session.flush()

    async def existing_ids(_module, _ids):
        return set()                        # Zoho has none of them
    monkeypatch.setattr(customer_admin.crm, "existing_ids", existing_ids)

    [row] = await customer_admin.list_customers(session, verify=True)
    assert row.zoho_state == customer_admin.ZOHO_MISSING
    assert customer.zoho_lead_id is None and order.zoho_order_id is None
    assert row.unsynced_orders == 1 and row.needs_push and not row.in_zoho


async def test_a_link_zoho_confirms_is_shown_as_in_zoho(
        session, customer, monkeypatch, zoho_online):
    customer.zoho_contact_id = "C1"
    asked = []

    async def existing_ids(module, ids):
        asked.append((module, ids))
        return {"C1"}
    monkeypatch.setattr(customer_admin.crm, "existing_ids", existing_ids)

    [row] = await customer_admin.list_customers(session, verify=True)
    assert asked == [("Contacts", ["C1"])]
    assert row.zoho_state == customer_admin.ZOHO_OK and row.in_zoho
    assert row.zoho_label == "Contact" and not row.needs_push


async def test_without_a_zoho_connection_the_page_says_so_and_asks_nothing(
        session, customer, monkeypatch):
    monkeypatch.setattr(customer_admin, "zoho_connected", lambda: False)
    customer.zoho_lead_id = "L1"

    async def existing_ids(_module, _ids):
        raise AssertionError("Zoho must not be called")
    monkeypatch.setattr(customer_admin.crm, "existing_ids", existing_ids)

    [row] = await customer_admin.list_customers(session, verify=True)
    assert row.zoho_state == customer_admin.ZOHO_OFFLINE
    assert customer.zoho_lead_id == "L1"          # nothing dropped blindly


async def test_zoho_being_unreachable_leaves_the_links_alone(
        session, customer, monkeypatch, zoho_online):
    customer.zoho_lead_id = "L1"

    async def existing_ids(_module, _ids):
        raise IntegrationError("zoho", "503")
    monkeypatch.setattr(customer_admin.crm, "existing_ids", existing_ids)

    [row] = await customer_admin.list_customers(session, verify=True)
    assert row.zoho_state == customer_admin.ZOHO_UNVERIFIED
    assert customer.zoho_lead_id == "L1" and not row.needs_push


async def test_forgetting_links_clears_every_saved_zoho_id(session, customer, outlet):
    customer.zoho_contact_id = "C1"
    order = await _order(session, customer, outlet, "SHO-TEST-7", paid=True,
                         hold_status=SlotHoldStatus.BOOKED)
    order.zoho_order_id = "O1"
    await session.flush()

    assert await customer_admin.forget_zoho_links(session) == 1
    assert customer.zoho_contact_id is None and customer.zoho_lead_id is None
    assert order.zoho_order_id is None
