"""Zoho sync as the spec describes it (section 2).

A customer is a Lead through the funnel, becomes a Contact on their first
payment, and every paid order is filed under that Contact.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.config import settings
from app.core.exceptions import IntegrationError
from app.db.models import DeliverySlot, LeadStage, Order, OrderStage, PaymentStatus
from app.integrations.zoho import crm, schema
from app.integrations.zoho import fields as f
from app.services import crm_sync

pytestmark = pytest.mark.asyncio


@pytest.fixture
def zoho(monkeypatch):
    """A fake Zoho: records every write, and holds what a phone search finds."""
    class Calls(list):
        found: dict = {}                 # module -> the record a search returns
        convert_error: Exception | None = None

    calls = Calls()
    calls.found = {}

    async def find_by_phone(module, _number):
        return calls.found.get(module)

    async def get_record(_module, zoho_id):
        return {"id": zoho_id}

    async def create_lead(fields):
        calls.append(("create_lead", fields))
        return "lead-1"

    async def update_record(module, zoho_id, fields):
        calls.append(("update", module, zoho_id, fields))

    async def convert_lead(lead_id):
        if calls.convert_error:
            raise calls.convert_error
        calls.append(("convert", lead_id))
        return "contact-1"

    async def relink_order(zoho_order_id, contact_id):
        calls.append(("relink", zoho_order_id, contact_id))

    async def create_order(fields):
        calls.append(("create_order", fields))
        return "zorder-1"

    async def update_order(zoho_order_id, fields):
        calls.append(("update_order", zoho_order_id, fields))

    async def update_order_stage(zoho_id, stage, *, at=None):
        calls.append(("order_stage", zoho_id, stage))

    async def create_order_item(fields):
        calls.append(("create_item", fields))
        return f"zitem-{len(_writes(calls, 'create_item'))}"

    async def update_order_item(zoho_item_id, fields):
        calls.append(("update_item", zoho_item_id, fields))

    async def find_product_by_code(_code):
        return None

    async def create_product(fields):
        calls.append(("create_product", fields))
        return f"prod-{len(_writes(calls, 'create_product'))}"

    async def update_product(zoho_id, fields):
        calls.append(("update_product", zoho_id, fields))

    async def find_vendor(_code, _name):
        return None

    async def create_vendor(fields):
        calls.append(("create_vendor", fields))
        return "vendor-1"

    async def update_vendor(zoho_id, fields):
        calls.append(("update_vendor", zoho_id, fields))

    for name, fn in {"find_by_phone": find_by_phone, "get_record": get_record,
                     "create_lead": create_lead, "update_record": update_record,
                     "convert_lead": convert_lead, "create_order": create_order,
                     "update_order": update_order,
                     "update_order_stage": update_order_stage,
                     "relink_order": relink_order,
                     "create_order_item": create_order_item,
                     "update_order_item": update_order_item,
                     "find_product_by_code": find_product_by_code,
                     "create_product": create_product, "update_product": update_product,
                     "find_vendor": find_vendor, "create_vendor": create_vendor,
                     "update_vendor": update_vendor}.items():
        monkeypatch.setattr(crm_sync.crm, name, fn)
    return calls


def _writes(calls, kind):
    return [c for c in calls if c[0] == kind]


def _ctx(outlet=None, dishes=None) -> crm_sync.OrderContext:
    """An order's context without a kitchen or menu rows, for tests of the
    customer side; the order-item tests build a real one from the session."""
    return crm_sync.OrderContext(outlet=outlet, dishes=dishes or {})


# --- the Lead ----------------------------------------------------------------
async def test_a_new_number_becomes_a_lead(customer, zoho):
    customer.name = "Nagi Pragalathan"
    customer.ad_id, customer.campaign_id = "ad-9", "camp-3"
    assert await crm_sync.ensure_record(customer) == (f.LEADS, "lead-1")

    (_, fields), = _writes(zoho, "create_lead")
    assert fields[f.FIRST_NAME] == "Nagi" and fields[f.LAST_NAME] == "Pragalathan"
    assert fields[f.PHONE] == "+" + customer.whatsapp_number.lstrip("+")
    assert fields[f.LEAD_SOURCE] == "Meta Ad"          # the fixture customer came from an ad
    assert fields[f.AD_ID] == "ad-9" and fields[f.CAMPAIGN_ID] == "camp-3"
    assert fields[f.BOT_STAGE] == customer.lead_stage
    assert customer.zoho_lead_id == "lead-1" and customer.zoho_contact_id is None


async def test_a_customer_who_just_messaged_has_whatsapp_as_the_source(customer, zoho):
    customer.lead_source = "WhatsApp"
    await crm_sync.ensure_record(customer)
    (_, fields), = _writes(zoho, "create_lead")
    assert fields[f.LEAD_SOURCE] == "WhatsApp"


async def test_a_number_already_in_leads_is_linked_not_duplicated(customer, zoho):
    zoho.found[f.LEADS] = {"id": "lead-old"}
    assert await crm_sync.ensure_record(customer) == (f.LEADS, "lead-old")
    assert _writes(zoho, "create_lead") == []


async def test_a_number_that_is_already_a_contact_is_greeted_by_name(customer, zoho):
    """Spec step 2: a returning (paid) customer, even after our DB was cleared."""
    customer.name = customer.email = None
    zoho.found[f.CONTACTS] = {"id": "c-9", "First_Name": "Asha", "Last_Name": "Menon",
                              "Email": "asha@example.com"}

    assert await crm_sync.ensure_record(customer) == (f.CONTACTS, "c-9")
    assert customer.zoho_contact_id == "c-9" and customer.is_converted
    assert customer.name == "Asha Menon" and customer.email == "asha@example.com"
    assert customer.has_details            # onboarding skips the name and email steps
    assert _writes(zoho, "create_lead") == []


async def test_the_placeholder_name_is_never_adopted(customer, zoho):
    customer.name = None
    zoho.found[f.LEADS] = {"id": "lead-old", "Last_Name": f.NAME_PLACEHOLDER}
    await crm_sync.ensure_record(customer)
    assert customer.name is None


# --- stages and details --------------------------------------------------------
async def test_a_stage_change_updates_bot_stage_and_its_history(customer, zoho):
    customer.zoho_lead_id = "lead-1"
    customer.lead_stage = str(LeadStage.CART_CREATED)
    customer.stage_timestamps = {"Cart Created": "2026-01-05T10:00:00+00:00"}

    await crm_sync.advance_stage(customer, LeadStage.OUTLET_SELECTED, forward_only=True)

    (_, module, zoho_id, fields), = _writes(zoho, "update")
    assert (module, zoho_id) == (f.LEADS, "lead-1")
    assert fields[f.BOT_STAGE] == "Outlet Selected"
    assert fields[f.LEAD_STATUS] == "Outlet Selected"     # Zoho's own stage bar too
    history = fields[f.BOT_STAGE_HISTORY].splitlines()
    assert history[0] == "2026-01-05 10:00 UTC  Cart Created"
    assert history[-1].endswith("Outlet Selected")


async def test_a_paid_customers_stages_go_to_their_contact(customer, zoho):
    customer.zoho_contact_id = "c-1"
    customer.lead_stage = str(LeadStage.CONVERTED)
    await crm_sync.advance_stage(customer, LeadStage.CART_CREATED)      # next order
    (_, module, zoho_id, fields), = _writes(zoho, "update")
    assert (module, zoho_id) == (f.CONTACTS, "c-1")
    assert fields[f.BOT_STAGE] == "Cart Created"
    assert f.LEAD_STATUS not in fields                    # a Contact has no Lead Status


@pytest.mark.parametrize("module,street,zip_code", [
    (f.LEADS, "Street", "Zip_Code"),
    (f.CONTACTS, "Mailing_Street", "Mailing_Zip"),
])
async def test_the_address_lands_on_each_modules_own_fields(
        customer, zoho, module, street, zip_code):
    if module == f.LEADS:
        customer.zoho_lead_id = "z-1"
    else:
        customer.zoho_contact_id = "z-1"
    await crm_sync.push_details(customer, address="12 Maple St", apartment_unit="2B",
                                postal_code="08820", latitude=40.5, longitude=-74.41,
                                outlet_name="Shero Edison", distance_km=3.2,
                                cuisine="north-indian", unknown="ignored")

    (_, mod, _, fields), = _writes(zoho, "update")
    assert mod == module
    assert fields[street] == "12 Maple St" and fields[zip_code] == "08820"
    assert fields[f.ADDRESS_LINE_2] == "2B"
    assert fields[f.LATITUDE] == "40.500000" and fields[f.LONGITUDE] == "-74.410000"
    assert fields[f.SELECTED_OUTLET] == "Shero Edison"
    assert fields[f.DISTANCE_KM] == 3.2
    assert fields[f.CUISINE] == "North Indian"
    assert "unknown" not in fields


async def test_zoho_being_down_never_breaks_the_conversation(customer, monkeypatch):
    async def down(_module, _phone):
        raise IntegrationError("zoho", "503")
    monkeypatch.setattr(crm_sync.crm, "find_by_phone", down)

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


async def test_the_first_web_dish_counts_as_cuisine_selected_too(customer, zoho):
    """The web menu has no cuisine step; the funnel must not skip it."""
    customer.lead_stage = str(LeadStage.DETAILS_CAPTURED)
    customer.zoho_lead_id = "lead-1"
    crm_sync.note_passed(customer, LeadStage.CUISINE_SELECTED)
    await crm_sync.advance_stage(customer, LeadStage.CART_CREATED, forward_only=True)

    assert str(LeadStage.CUISINE_SELECTED) in customer.stage_timestamps
    [(_, _, _, fields)] = zoho            # one Zoho call carries both stages
    assert fields[f.BOT_STAGE] == "Cart Created"
    assert "Cuisine Selected" in fields[f.BOT_STAGE_HISTORY]

    # Later dishes, after a slot is picked, do not rewind it.
    customer.lead_stage = str(LeadStage.SLOT_SELECTED)
    crm_sync.note_passed(customer, LeadStage.CUISINE_SELECTED)
    assert customer.lead_stage == str(LeadStage.SLOT_SELECTED)


# --- payment: Lead -> Contact, and the Order -----------------------------------
def _paid_order(customer) -> Order:
    return Order(order_number="SHO-TEST-9", customer_id=customer.id,
                 items=[{"retailer_id": "kerala-sambar-sambar", "name": "Sambar",
                         "quantity": 2, "unit_price": "9.50"}],
                 item_count=2, dish_total=Decimal("19.00"), delivery_fee=Decimal("4.00"),
                 extra_fees=Decimal("1.00"), tax=Decimal("1.50"), total=Decimal("25.50"),
                 delivery_address="12 Maple St", apartment_unit="2B", postal_code="08820",
                 delivery_latitude=40.52, delivery_longitude=-74.41, distance_km=3.2,
                 contact_number="917401268091", slot_label="Fri 7:00 PM - 8:00 PM",
                 stripe_payment_intent_id="pi_1",
                 created_at=datetime(2026, 9, 28, 10, 0, 5, 123, tzinfo=timezone.utc),
                 paid_at=datetime(2026, 9, 28, 10, 5, tzinfo=timezone.utc),
                 payment_status=PaymentStatus.PAID, stage=OrderStage.PAID_SLOT_BOOKED)


async def test_payment_converts_the_lead_and_files_the_order_under_the_contact(
        session, customer, zoho):
    customer.zoho_lead_id = "lead-1"
    customer.cuisine_preference = "kerala"
    order = _paid_order(customer)
    session.add(order)
    await session.flush()

    await crm_sync.convert_and_record_order(customer, order, _ctx())

    (_, lead_id), = _writes(zoho, "convert")
    assert lead_id == "lead-1"
    # The bot's fields land on the new Contact right after the conversion.
    (_, module, zoho_id, contact_fields), = _writes(zoho, "update")
    assert (module, zoho_id) == (f.CONTACTS, "contact-1")
    assert contact_fields[f.BOT_STAGE] == "Converted"
    assert f.LEAD_STATUS not in contact_fields
    assert contact_fields["Mailing_Street"] == "12 Maple St"     # the address it went to
    assert contact_fields[f.CUISINE] == "Kerala"
    assert contact_fields[f.DISTANCE_KM] == 3.2
    assert customer.zoho_contact_id == "contact-1" and customer.zoho_lead_id is None
    assert customer.is_converted

    (_, o), = _writes(zoho, "create_order")
    assert o[settings.zoho_orders_name_field] == "SHO-TEST-9"
    assert o[f.O_CONTACT] == {"id": "contact-1"} and o[f.O_LEAD] is None
    assert o[f.O_CHANNEL] == "WhatsApp Bot"
    assert o[f.O_STATUS] == "Paid & Slot Booked"
    assert o[f.O_ITEMS] == "2 x Sambar @ 9.50 = 19.00"
    assert o[f.O_DISH_TOTAL] == Decimal("19.00") and o[f.O_ORDER_TOTAL] == Decimal("25.50")
    assert o[f.O_DELIVERY_CHARGE] == Decimal("4.00") and o[f.O_TAXES_AND_FEES] == Decimal("2.50")
    assert o[f.O_ADDRESS] == "12 Maple St, 2B, 08820"
    assert o[f.O_LATITUDE] == "40.520000"
    assert o[f.O_CUSTOMER_NO] == "+917401268091"
    assert o[f.O_PLACED_TIME] == "2026-09-28T10:00:05+00:00"     # no microseconds
    assert o[f.O_PAID_TIME] == "2026-09-28T10:05:00+00:00"
    assert o[f.O_STRIPE_PAYMENT_ID] == "pi_1"
    assert order.zoho_order_id == "zorder-1"

    # A replayed payment must not file it twice.
    await crm_sync.convert_and_record_order(customer, order, _ctx())
    assert len(_writes(zoho, "create_order")) == 1


async def test_a_second_order_goes_under_the_existing_contact(session, customer, zoho):
    customer.zoho_contact_id = "c-1"
    order = _paid_order(customer)
    session.add(order)
    await session.flush()

    await crm_sync.convert_and_record_order(customer, order, _ctx())

    assert _writes(zoho, "convert") == []
    (_, o), = _writes(zoho, "create_order")
    assert o[f.O_CONTACT] == {"id": "c-1"}


async def test_a_refused_conversion_keeps_the_lead_and_still_files_the_order(
        session, customer, zoho):
    customer.zoho_lead_id = "lead-1"
    zoho.convert_error = IntegrationError("zoho", "conversion not allowed")
    order = _paid_order(customer)
    session.add(order)
    await session.flush()

    await crm_sync.convert_and_record_order(customer, order, _ctx())

    assert customer.zoho_lead_id == "lead-1" and customer.zoho_contact_id is None
    (_, module, _, fields), = _writes(zoho, "update")
    assert module == f.LEADS and fields[f.BOT_STAGE] == "Converted"
    (_, o), = _writes(zoho, "create_order")
    assert o[f.O_LEAD] == {"id": "lead-1"} and o[f.O_CONTACT] is None


async def test_the_admin_push_converts_a_paid_customer_still_held_as_a_lead(
        customer, zoho):
    customer.is_converted = True
    customer.lead_stage = str(LeadStage.CONVERTED)
    customer.zoho_lead_id = "lead-1"

    problems = await crm_sync.push_customer(customer, details={"name": "Asha Menon"},
                                            orders=[])
    assert problems == []
    assert len(_writes(zoho, "convert")) == 1
    assert customer.zoho_contact_id == "contact-1"


async def test_the_admin_push_moves_orders_filed_under_the_lead_to_the_contact(
        customer, zoho):
    """While the conversion kept failing, orders were filed under the Lead."""
    customer.is_converted = True
    customer.lead_stage = str(LeadStage.CONVERTED)
    customer.zoho_lead_id = "lead-1"
    order = _paid_order(customer)
    order.zoho_order_id = "zorder-9"

    problems = await crm_sync.push_customer(customer, details={"name": "Asha Menon"},
                                            orders=[(order, _ctx())])
    assert problems == []
    assert _writes(zoho, "relink") == [("relink", "zorder-9", "contact-1")]
    assert _writes(zoho, "create_order") == []          # already filed, not filed twice

    # A push on a customer who was already a Contact leaves the orders alone.
    zoho.clear()
    await crm_sync.push_customer(customer, details={"name": "Asha Menon"},
                                 orders=[(order, _ctx())])
    assert _writes(zoho, "relink") == []


async def test_a_lead_converted_by_hand_in_zoho_is_followed_to_its_contact(
        customer, zoho, monkeypatch):
    """Staff pressed Convert in Zoho: writes to the Lead are refused from then on."""
    customer.zoho_lead_id = "lead-1"
    zoho.found[f.CONTACTS] = {"id": "c-7", "First_Name": "Asha", "Last_Name": "Menon"}

    async def update_record(module, zoho_id, fields):
        if module == f.LEADS:
            raise IntegrationError("zoho", "record write failed", payload={
                "code": "INVALID_DATA", "message": "can't update the converted record"})
        zoho.append(("update", module, zoho_id, fields))
    monkeypatch.setattr(crm_sync.crm, "update_record", update_record)

    await crm_sync.advance_stage(customer, LeadStage.CART_CREATED)

    (_, module, zoho_id, fields), = _writes(zoho, "update")
    assert (module, zoho_id) == (f.CONTACTS, "c-7")
    assert fields[f.BOT_STAGE] == "Cart Created" and f.LEAD_STATUS not in fields
    assert customer.zoho_contact_id == "c-7" and customer.zoho_lead_id is None


async def test_the_admin_push_follows_a_converted_lead_and_moves_its_orders(
        customer, zoho, monkeypatch):
    customer.is_converted = True
    customer.lead_stage = str(LeadStage.CONVERTED)
    customer.zoho_lead_id = "lead-1"
    zoho.found[f.CONTACTS] = {"id": "c-7"}
    zoho.convert_error = IntegrationError("zoho", "already converted")
    order = _paid_order(customer)
    order.zoho_order_id = "zorder-9"

    async def update_record(module, zoho_id, fields):
        if module == f.LEADS:
            raise IntegrationError("zoho", "record write failed", payload={
                "code": "INVALID_DATA", "message": "can't update the converted record"})
        zoho.append(("update", module, zoho_id, fields))
    monkeypatch.setattr(crm_sync.crm, "update_record", update_record)

    problems = await crm_sync.push_customer(customer, details={"name": "Asha Menon"},
                                            orders=[(order, _ctx())])
    assert problems == []
    assert customer.zoho_contact_id == "c-7"
    assert _writes(zoho, "relink") == [("relink", "zorder-9", "c-7")]


# --- Order Items, Products and Vendors ------------------------------------------
async def test_payment_files_one_order_item_per_dish_linked_to_its_product(
        session, customer, outlet, menu, zoho):
    customer.zoho_contact_id = "c-1"
    order = _paid_order(customer)
    order.outlet_id = outlet.id
    order.items = [
        {"retailer_id": "kerala-sambar-appam", "name": "Appam", "quantity": 3,
         "unit_price": "3.25"},
        {"retailer_id": "chettinad-sambar-drumstick-sambar", "name": "Drumstick Sambar",
         "quantity": 1, "unit_price": "12.50"},
    ]
    session.add(order)
    await session.flush()
    ctx = await crm_sync.order_context(session, order)
    assert ctx.outlet is outlet
    assert set(ctx.dishes) == {"kerala-sambar-appam", "chettinad-sambar-drumstick-sambar"}

    await crm_sync.convert_and_record_order(customer, order, ctx)

    # The kitchen became a Vendor, and the Order points at it.
    (_, vendor), = _writes(zoho, "create_vendor")
    assert vendor[f.V_NAME] == "Shero Edison" and vendor[f.V_OUTLET_CODE] == "EDISON"
    assert vendor[f.V_COUNTRY] == "United States" and vendor[f.V_CITY] == "Edison"
    assert outlet.zoho_vendor_id == "vendor-1"
    (_, o), = _writes(zoho, "create_order")
    assert o[f.O_OUTLET] == {"id": "vendor-1"} and o[f.O_OUTLET_NAME] == "Shero Edison"

    # Each dish became a Product, remembered on the menu row ...
    products = {p[f.P_CODE]: p for _, p in _writes(zoho, "create_product")}
    assert set(products) == set(ctx.dishes)
    appam = products["kerala-sambar-appam"]
    assert appam[f.P_NAME] == "Appam" and appam[f.P_UNIT_PRICE] == Decimal("3.25")
    assert appam[f.P_CUISINE] == "Kerala" and appam[f.P_DISH_CATEGORY] == "Sambar"
    assert appam[f.P_ACTIVE] is True
    assert menu["kerala-sambar-appam"].zoho_product_id == "prod-1"

    # ... and each cart line an Order Item linked to it, the Order and the Contact.
    items = [i for _, i in _writes(zoho, "create_item")]
    assert [i[f.I_NAME_DEFAULT] for i in items] == [
        "SHO-TEST-9 / Appam", "SHO-TEST-9 / Drumstick Sambar"]
    assert items[0][f.I_ORDER] == {"id": "zorder-1"}
    assert items[0][f.I_PRODUCT] == {"id": "prod-1"} and items[1][f.I_PRODUCT] == {"id": "prod-2"}
    assert items[0][f.I_CONTACT] == {"id": "c-1"}
    assert items[0][f.I_DISH_CODE] == "kerala-sambar-appam"
    assert items[0][f.I_QUANTITY] == 3 and items[0][f.I_UNIT_PRICE] == Decimal("3.25")
    assert items[0][f.I_LINE_TOTAL] == Decimal("9.75")
    assert order.zoho_item_ids == {"kerala-sambar-appam": "zitem-1",
                                   "chettinad-sambar-drumstick-sambar": "zitem-2"}


async def test_a_dish_no_longer_on_the_menu_is_filed_without_a_product_link(
        session, customer, zoho):
    customer.zoho_contact_id = "c-1"
    order = _paid_order(customer)       # "kerala-sambar-sambar" is not on the test menu
    session.add(order)
    await session.flush()

    await crm_sync.convert_and_record_order(
        customer, order, await crm_sync.order_context(session, order))

    assert _writes(zoho, "create_product") == []
    (_, item), = _writes(zoho, "create_item")
    assert item[f.I_PRODUCT] is None and item[f.I_DISH_CODE] == "kerala-sambar-sambar"
    assert item[f.I_NAME_DEFAULT] == "SHO-TEST-9 / Sambar"


async def test_a_push_updates_filed_order_items_instead_of_filing_them_again(
        session, customer, outlet, menu, zoho):
    customer.is_converted = True
    customer.lead_stage = str(LeadStage.CONVERTED)
    customer.zoho_contact_id = "c-1"
    outlet.zoho_vendor_id = "vendor-old"
    order = _paid_order(customer)
    order.outlet_id = outlet.id
    order.items = [{"retailer_id": "kerala-sambar-appam", "name": "Appam", "quantity": 1,
                    "unit_price": "3.25"}]
    order.zoho_order_id = "zorder-9"
    order.zoho_item_ids = {"kerala-sambar-appam": "zitem-7"}
    session.add(order)
    await session.flush()
    ctx = await crm_sync.order_context(session, order)

    problems = await crm_sync.push_customer(customer, details={"name": "Asha Menon"},
                                            orders=[(order, ctx)])

    assert problems == []
    assert _writes(zoho, "create_order") == [] and _writes(zoho, "create_item") == []
    assert _writes(zoho, "create_vendor") == []
    (_, vendor_id, _), = _writes(zoho, "update_vendor")
    assert vendor_id == "vendor-old"
    (_, zoho_id, link), = _writes(zoho, "update_order")
    assert zoho_id == "zorder-9" and link[f.O_OUTLET] == {"id": "vendor-old"}
    (_, item_id, item), = _writes(zoho, "update_item")
    assert item_id == "zitem-7"
    assert item[f.I_PRODUCT] == {"id": "prod-1"} and item[f.I_CONTACT] == {"id": "c-1"}


async def test_delivery_time_is_the_slots_start(session, customer, outlet, zoho):
    start = datetime(2026, 10, 2, 23, 0, tzinfo=timezone.utc)
    slot = DeliverySlot(outlet_id=outlet.id, starts_at=start,
                        ends_at=start + timedelta(hours=1), capacity=2)
    session.add(slot)
    await session.flush()
    customer.zoho_contact_id = "c-1"
    order = _paid_order(customer)
    order.outlet_id, order.slot_id = outlet.id, slot.id
    session.add(order)
    await session.flush()

    ctx = await crm_sync.order_context(session, order)
    assert ctx.delivery_at == start
    await crm_sync.convert_and_record_order(customer, order, ctx)
    (_, o), = _writes(zoho, "create_order")
    assert o[f.O_DELIVERY_TIME] == "2026-10-02T23:00:00+00:00"


async def test_an_order_item_failure_is_reported_without_losing_the_order(
        session, customer, zoho, monkeypatch):
    customer.is_converted = True
    customer.lead_stage = str(LeadStage.CONVERTED)
    customer.zoho_contact_id = "c-1"
    order = _paid_order(customer)
    session.add(order)
    await session.flush()

    async def create_order_item(_fields):
        raise IntegrationError("zoho", "INVALID_DATA")
    monkeypatch.setattr(crm_sync.crm, "create_order_item", create_order_item)

    problems = await crm_sync.push_customer(customer, details={"name": "Asha Menon"},
                                            orders=[(order, _ctx())])
    assert order.zoho_order_id == "zorder-1"
    assert problems == ["Order SHO-TEST-9: Sambar could not be filed in Order Items "
                        "- see the server log."]


def test_order_item_lookups_point_at_the_modules_they_link():
    lookups = {api: body["lookup"]["module"]["api_name"]
               for api, body in schema.ORDER_ITEM_FIELDS if body["data_type"] == "lookup"}
    assert lookups == {f.I_ORDER: schema.ORDERS, f.I_PRODUCT: f.PRODUCTS,
                       f.I_CONTACT: f.CONTACTS}
    outlet = next(body for api, body in schema.ORDER_FIELDS if api == f.O_OUTLET)
    assert outlet["lookup"]["module"]["api_name"] == f.VENDORS


# --- the Orders module ---------------------------------------------------------
def test_order_status_offers_every_order_stage():
    options = next(body for api, body in schema.ORDER_FIELDS if api == f.O_STATUS)
    assert [o["actual_value"] for o in options["pick_list_values"]] == [
        s.value for s in OrderStage]


async def test_delivery_updates_move_the_zoho_order(customer, zoho):
    order = _paid_order(customer)
    order.zoho_order_id = "zorder-1"
    await crm_sync.push_order_stage(order, OrderStage.OUT_FOR_DELIVERY)
    assert _writes(zoho, "order_stage") == [("order_stage", "zorder-1",
                                             OrderStage.OUT_FOR_DELIVERY)]


# --- the Zoho layer itself ------------------------------------------------------
def test_every_field_gets_the_api_name_zoho_derives_from_its_label():
    for spec in schema.FIELDS:
        assert schema.api_name_for(spec.body["field_label"]) == spec.api_name, spec


async def test_the_phone_search_matches_phone_or_mobile_with_or_without_the_plus(
        monkeypatch):
    seen = {}

    async def search(module, criteria):
        seen.update(module=module, criteria=criteria)
        return []
    monkeypatch.setattr(crm.zoho_client, "search", search)

    assert await crm.find_by_phone(f.CONTACTS, "917401268091") is None
    assert seen["module"] == "Contacts"
    for clause in ("(Phone:equals:+917401268091)", "(Phone:equals:917401268091)",
                   "(Mobile:equals:+917401268091)", "(Mobile:equals:917401268091)"):
        assert clause in seen["criteria"]


async def test_a_required_company_is_filled_with_the_customers_name(monkeypatch):
    """Zoho's default Leads layout requires Company; the bot must not stall."""
    attempts = []

    async def create_record(_module, fields):
        attempts.append(fields)
        if len(attempts) == 1:
            raise IntegrationError("zoho", "record write failed", payload={
                "code": "MANDATORY_NOT_FOUND", "details": {"api_name": "Company"}})
        return "lead-2"
    monkeypatch.setattr(crm.zoho_client, "create_record", create_record)

    lead_id = await crm.create_lead({f.FIRST_NAME: "Asha", f.LAST_NAME: "Menon"})
    assert lead_id == "lead-2"
    assert f.COMPANY not in attempts[0] and attempts[1][f.COMPANY] == "Asha Menon"


@pytest.mark.parametrize("reply,contact_id", [
    # v6+: ids under "details"; older versions at the top level, v2 as bare strings.
    ({"data": [{"code": "SUCCESS", "status": "success",
                "details": {"Contacts": {"id": "333"}, "Deals": None, "Accounts": None}}]}, "333"),
    ({"data": [{"Contacts": {"id": "111"}, "Deals": None, "Accounts": None}]}, "111"),
    ({"data": [{"Contacts": "222", "Deals": None, "Accounts": None}]}, "222"),
])
async def test_conversion_reads_the_contact_id_in_either_reply_shape(
        monkeypatch, reply, contact_id):
    async def post(_url, **_kw):
        return reply
    monkeypatch.setattr(crm.zoho_client, "post", post)
    assert await crm.zoho_client.convert_lead("lead-1") == contact_id


async def test_a_conversion_error_is_raised_not_swallowed(monkeypatch):
    async def post(_url, **_kw):
        return {"data": [{"status": "error", "code": "INVALID_DATA", "message": "no"}]}
    monkeypatch.setattr(crm.zoho_client, "post", post)
    with pytest.raises(IntegrationError):
        await crm.zoho_client.convert_lead("lead-1")
