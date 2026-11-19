"""DST India's customers pay Shiprocket's live rider price for their drop,
never a flat or guessed charge (services/pricing.py: rider_price)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core import tenants
from app.core.config import settings_for
from app.integrations.shiprocket import client as sr
from app.services import orders, pricing, settings_store

D = Decimal
SAMBAR = "chettinad-sambar-drumstick-sambar"
DROP = {"street_address": ["12 2nd Avenue, Anna Nagar"], "zip_code": "600040"}
# What the live account answered for a 2.7 km drop in Chennai (19 Nov 2026).
QUICK = {"status": True, "data": [{"courier_name": "Shiprocket Quick", "rates": 65, "etd_hours": 1,
                                   "distance": 2.72}]}


@pytest.fixture
def connected(monkeypatch):
    """DST India with a Shiprocket API user and a flat fee that must go unused."""
    dst = settings_for(tenants.DST)
    monkeypatch.setattr(dst, "shiprocket_email", "bot@shero.test")
    monkeypatch.setattr(dst, "shiprocket_password", "not-a-real-password")
    monkeypatch.setattr(dst, "own_delivery_fee", 40.0)
    monkeypatch.setattr(dst, "free_delivery_above", 0.0)
    monkeypatch.setattr(dst, "tax_percent", 5.0)
    monkeypatch.setattr(pricing, "_rider_prices", {})
    with tenants.use(tenants.DST):
        yield dst


@pytest.fixture
def rates(monkeypatch):
    """Shiprocket's rates call, faked: records each ask, answers `answer`."""
    class Rates:
        answer: object = sr.couriers(QUICK)
        asked: list[dict] = []

    Rates.asked = []

    async def hyperlocal_rates(**kw):
        Rates.asked.append(kw)
        if isinstance(Rates.answer, Exception):
            raise Rates.answer
        return Rates.answer

    monkeypatch.setattr(sr, "hyperlocal_rates", hyperlocal_rates)
    return Rates


async def _price(session, outlet, quantity=2, *, lat=13.0850, lng=80.2101, drop=DROP):
    return await pricing.price_cart(
        session, [{"retailer_id": SAMBAR, "quantity": quantity}], outlet=outlet,
        dropoff_address=drop, dropoff_latitude=lat, dropoff_longitude=lng)


async def test_the_customer_pays_shiprocket_live_price_not_the_flat_fee(session, menu, outlet,
                                                                       connected, rates):
    priced = await _price(session, outlet)
    assert priced.delivery_quote_failed is False
    assert priced.delivery_fee == D("65.00")                 # Shiprocket's, not the flat 40
    assert priced.rider_quote["fee"] == 65.0 and priced.rider_quote["courier"] == "Shiprocket Quick"
    assert priced.total == priced.dish_total + D("65.00") + priced.tax
    asked = rates.asked[0]
    assert (asked["lat_from"], asked["long_from"]) == (outlet.latitude, outlet.longitude)
    assert (asked["lat_to"], asked["long_to"]) == (13.0850, 80.2101)
    assert asked["pickup_postcode"] == outlet.postal_code and asked["delivery_postcode"] == "600040"


async def test_the_same_drop_keeps_its_price_so_confirm_charges_what_was_shown(session, menu, outlet,
                                                                               connected, rates):
    first = await _price(session, outlet)
    rates.answer = sr.couriers({"data": [{"courier_name": "Shiprocket Quick", "rates": 90}]})
    confirm = await _price(session, outlet)
    assert first.delivery_fee == confirm.delivery_fee == D("65.00")
    assert len(rates.asked) == 1
    elsewhere = await _price(session, outlet, lat=13.0500, lng=80.2500)
    assert elsewhere.delivery_fee == D("90.00") and len(rates.asked) == 2


@pytest.mark.parametrize("answer, why", [([], pricing.UNDELIVERABLE),
                                         (sr.ShiprocketError("Shiprocket answered 500"), pricing.UNAVAILABLE)])
async def test_no_live_price_means_no_order_never_the_flat_fee(session, menu, outlet, connected, rates,
                                                               answer, why):
    rates.answer = answer
    priced = await _price(session, outlet)
    assert priced.delivery_quote_failed is True and priced.delivery_quote_error == why
    assert priced.delivery_fee == D("0.00") and not priced.rider_quote


async def test_a_drop_without_a_map_pin_is_not_priced(session, menu, outlet, connected, rates):
    priced = await _price(session, outlet, lat=None, lng=None)
    assert priced.delivery_quote_failed is True and priced.delivery_quote_error == pricing.UNAVAILABLE
    assert rates.asked == []


async def test_free_delivery_above_an_amount_still_applies(session, menu, outlet, connected, rates,
                                                           monkeypatch):
    monkeypatch.setattr(connected, "free_delivery_above", 500.0)
    many = await _price(session, outlet, quantity=50)
    assert many.dish_total >= D("500") and many.delivery_fee == D("0.00")
    assert many.rider_quote["fee"] == 65.0                   # what the rider will cost the business


async def test_the_order_keeps_the_price_it_was_charged(session, menu, outlet, customer, connected,
                                                        rates):
    priced = await _price(session, outlet)
    order = await orders.create_draft_order(session, customer=customer, outlet=outlet, priced=priced)
    assert order.delivery_fee == D("65.00")
    assert order.courier["quote"]["fee"] == 65.0


def test_settings_hide_the_flat_charge_while_shiprocket_prices_delivery(connected):
    keys = {k for fields in settings_store.setting_groups().values() for k, _, _ in fields}
    assert "OWN_DELIVERY_FEE" not in keys and "FREE_DELIVERY_ABOVE" in keys


def test_without_shiprocket_the_flat_fee_is_still_offered():
    with tenants.use(tenants.DST):
        assert not pricing.live_rider_price()
        keys = {k for fields in settings_store.setting_groups().values() for k, _, _ in fields}
    assert "OWN_DELIVERY_FEE" in keys
    assert not pricing.live_rider_price()                    # Shero US: Uber's quote, as before
