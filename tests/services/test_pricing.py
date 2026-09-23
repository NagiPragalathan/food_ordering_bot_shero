"""Pricing: menu re-pricing, totals and the customer-facing summary.

Prices come from this service's own menu now (imported from the client's
sheet), not the Meta catalogue.
"""

from decimal import Decimal

import pytest

from app.core.exceptions import ItemUnavailableError
from app.services.pricing import (
    PricedLine,
    PricedOrder,
    format_summary,
    money,
    price_cart,
)

BIRYANI = "chettinad-sambar-drumstick-sambar"
BEANS = "chettinad-sambar-beans-sambar"
RASAM = "chettinad-rasam-tomato-rasam"


def test_money_rounds_half_up():
    # Half-up, not bankers' rounding: 0.125 must become 0.13, not 0.12.
    assert money("0.125") == Decimal("0.13")
    assert money("2.005") == Decimal("2.01")


async def test_prices_come_from_the_menu_not_the_cart(session, menu):
    """A cart claiming its own price must not influence what we charge."""
    priced = await price_cart(session, [
        {"retailer_id": BIRYANI, "quantity": 2, "unit_price": "0.01"},
    ])
    assert priced.dish_total == Decimal("25.00")
    assert priced.lines[0].unit_price == Decimal("12.50")
    assert priced.lines[0].name == "Drumstick Sambar"


async def test_totals_add_up_without_a_delivery_quote(session, menu):
    priced = await price_cart(session, [
        {"retailer_id": BIRYANI, "quantity": 1},
        {"retailer_id": RASAM, "quantity": 3},
    ])
    assert priced.dish_total == Decimal("41.00")   # 12.50 + 28.50
    assert priced.item_count == 4
    # No kitchen/address supplied, so no Uber quote was attempted.
    assert priced.delivery_fee == Decimal("0.00")
    assert priced.total == priced.dish_total + priced.tax


async def test_unavailable_item_is_named_in_the_customer_message(session, menu):
    """An item hidden in the admin dashboard is refused, by name not by slug."""
    menu[BEANS].is_available = False
    await session.flush()

    with pytest.raises(ItemUnavailableError) as excinfo:
        await price_cart(session, [{"retailer_id": BEANS, "quantity": 1}])
    assert "Beans Sambar" in excinfo.value.customer_message


async def test_unknown_retailer_id_is_rejected(session, menu):
    with pytest.raises(ItemUnavailableError):
        await price_cart(session, [{"retailer_id": "does-not-exist", "quantity": 1}])


async def test_empty_cart_is_rejected(session, menu):
    with pytest.raises(ItemUnavailableError):
        await price_cart(session, [])


async def test_zero_quantity_lines_are_dropped(session, menu):
    priced = await price_cart(session, [
        {"retailer_id": BIRYANI, "quantity": 1},
        {"retailer_id": RASAM, "quantity": 0},
    ])
    assert [line.retailer_id for line in priced.lines] == [BIRYANI]


async def test_tax_is_applied_to_the_dish_subtotal(session, menu, monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.settings, "tax_percent", 8.5)
    priced = await price_cart(session, [{"retailer_id": BIRYANI, "quantity": 1}])
    assert priced.tax == Decimal("1.06")          # 12.50 * 8.5%
    assert priced.total == Decimal("13.56")


async def test_cost_price_never_reaches_the_customer(session, menu):
    """PPP is internal. Nothing the customer is charged may derive from it."""
    priced = await price_cart(session, [{"retailer_id": BIRYANI, "quantity": 1}])
    assert priced.dish_total == Decimal("12.50")      # MRP
    assert priced.dish_total != Decimal("8.16")       # not PPP
    for line in priced.items_as_dicts():
        assert "cost" not in " ".join(line.keys()).lower()


def test_summary_lists_every_charge_line():
    priced = PricedOrder(
        lines=[PricedLine(BIRYANI, "Drumstick Sambar", 2,
                          Decimal("12.50"), Decimal("25.00"))],
        dish_total=Decimal("25.00"),
        delivery_fee=Decimal("5.99"),
        extra_fees=Decimal("1.20"),
        tax=Decimal("2.13"),
        total=Decimal("34.32"),
        currency="USD",
    )
    summary = format_summary(priced, outlet_name="Shero Home Food",
                             slot_label="Mon 22 Sep, 6:00 PM - 7:00 PM")

    assert "2 x Drumstick Sambar - $25.00" in summary
    assert "Delivery: $5.99" in summary
    assert "Taxes and fees: $1.20" in summary
    assert "*Total: $34.32*" in summary
    assert "Mon 22 Sep" in summary


def test_summary_hides_zero_value_lines():
    priced = PricedOrder(
        lines=[PricedLine(RASAM, "Tomato Rasam", 1,
                          Decimal("9.50"), Decimal("9.50"))],
        dish_total=Decimal("9.50"),
        total=Decimal("9.50"),
        currency="USD",
    )
    summary = format_summary(priced, outlet_name="Shero Home Food",
                             slot_label="today")
    assert "Delivery:" not in summary
    assert "Tax:" not in summary
