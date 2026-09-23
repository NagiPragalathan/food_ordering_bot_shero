"""Order numbering, creation and stage transitions."""

import re
from decimal import Decimal

from app.db.models import Order, OrderStage
from app.services.orders import (
    allocate_order_number,
    create_draft_order,
    generate_order_number,
    get_by_number,
    set_stage,
)
from app.services.pricing import PricedLine, PricedOrder

ORDER_NUMBER_RE = re.compile(r"^SHO-\d{6}-[0-9A-HJKMNP-TV-Z]{5}$")


def _priced() -> PricedOrder:
    return PricedOrder(
        lines=[PricedLine("biryani", "Chicken Biryani", 2,
                          Decimal("12.50"), Decimal("25.00"))],
        dish_total=Decimal("25.00"),
        delivery_fee=Decimal("5.99"),
        extra_fees=Decimal("1.20"),
        tax=Decimal("2.13"),
        total=Decimal("34.32"),
        currency="USD",
        uber_quote_id="dqt_1",
    )


def test_order_number_format():
    number = generate_order_number()
    assert ORDER_NUMBER_RE.match(number), number


def test_order_number_avoids_ambiguous_characters():
    """I, L, O and U are excluded so a number read aloud is unambiguous."""
    suffixes = "".join(generate_order_number().split("-")[2] for _ in range(200))
    assert not set(suffixes) & set("ILOU")


async def test_allocated_number_is_unique(session, customer):
    taken = await allocate_order_number(session)
    session.add(Order(order_number=taken, customer_id=customer.id))
    await session.flush()

    another = await allocate_order_number(session)
    assert another != taken


async def test_draft_order_snapshots_prices_and_address(session, customer, outlet):
    order = await create_draft_order(
        session, customer=customer, outlet=outlet, priced=_priced(),
        slot_label="Mon 22 Sep, 6:00 PM - 7:00 PM", distance_km=3.2,
    )

    assert order.dish_total == Decimal("25.00")
    assert order.delivery_fee == Decimal("5.99")
    assert order.extra_fees == Decimal("1.20")
    assert order.total == Decimal("34.32")
    assert order.item_count == 2
    assert order.items[0]["name"] == "Chicken Biryani"
    # Address is copied onto the order, not just referenced.
    assert order.delivery_address == "12 Maple Street"
    assert order.contact_number == "17325550142"
    assert order.stage == OrderStage.PENDING_PAYMENT
    assert order.uber_quote_id == "dqt_1"


async def test_amount_in_minor_units_matches_the_total(session, customer, outlet):
    order = await create_draft_order(
        session, customer=customer, outlet=outlet, priced=_priced())
    assert order.amount_minor_units == 3432


async def test_order_is_findable_by_number(session, customer, outlet):
    order = await create_draft_order(
        session, customer=customer, outlet=outlet, priced=_priced())
    found = await get_by_number(session, order.order_number)
    assert found is not None
    assert found.id == order.id


def test_set_stage_records_a_timestamp_and_reports_the_change():
    order = Order(order_number="SHO-260922-AAAAA", stage=OrderStage.PENDING_PAYMENT,
                  stage_timestamps={})

    assert set_stage(order, OrderStage.PAID_SLOT_BOOKED) is True
    assert order.stage == OrderStage.PAID_SLOT_BOOKED
    assert order.paid_at is not None
    assert str(OrderStage.PAID_SLOT_BOOKED) in order.stage_timestamps


def test_set_stage_is_a_no_op_when_the_stage_is_unchanged():
    """A repeated webhook must not re-notify or re-stamp."""
    order = Order(order_number="SHO-260922-AAAAA", stage=OrderStage.DELIVERED,
                  stage_timestamps={})
    assert set_stage(order, OrderStage.DELIVERED) is False


def test_stage_timestamps_accumulate_across_the_lifecycle():
    order = Order(order_number="SHO-260922-AAAAA",
                  stage=OrderStage.PENDING_PAYMENT, stage_timestamps={})

    for stage in (OrderStage.PAID_SLOT_BOOKED, OrderStage.SENT_TO_KITCHEN,
                  OrderStage.OUT_FOR_DELIVERY, OrderStage.DELIVERED):
        set_stage(order, stage)

    assert set(order.stage_timestamps) == {
        str(OrderStage.PAID_SLOT_BOOKED), str(OrderStage.SENT_TO_KITCHEN),
        str(OrderStage.OUT_FOR_DELIVERY), str(OrderStage.DELIVERED),
    }
    assert order.delivered_at is not None
    assert order.out_for_delivery_at is not None


def test_first_timestamp_for_a_stage_is_kept():
    """Re-entering a stage must not overwrite when it first happened."""
    order = Order(order_number="SHO-260922-AAAAA",
                  stage=OrderStage.PENDING_PAYMENT, stage_timestamps={})
    set_stage(order, OrderStage.DELIVERED)
    first_delivered_at = order.delivered_at

    set_stage(order, OrderStage.CANCELLED)
    set_stage(order, OrderStage.DELIVERED)
    assert order.delivered_at == first_delivered_at
