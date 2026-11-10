"""Uber prices and books the same delivery (services/delivery_address.py,
pricing._quote_delivery, dispatch.dispatch).

SHO-261108-7QW2Q: Uber refused the quote - the address was 23.71 miles away,
beyond its 10-mile radius (our own check had measured a map pin left by the
kitchen; tests/services/test_pin_vs_address.py). The page ignored the refusal,
charged no delivery, took the payment, and the booking then failed every 5
minutes until the slot was over. What must hold now:
the quote and the booking send the same, complete address and window; no
Uber price means no order; and a booking Uber refuses for good is not retried.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.api.routes import order_web
from app.core.config import settings
from app.core.exceptions import IntegrationError
from app.db.models import Order
from app.integrations.geo.geocoder import GeoPoint
from app.integrations.gallabox.sender import use_sender
from app.integrations.uber import direct as uber
from app.services import addresses, delivery_address, dispatch, order_link, pricing
from tests.services.test_dispatch import NOW, _order
from tests.services.test_out_of_area import NEAR, FakeRequest, Recorder

REFUSAL = {"code": "address_undeliverable",
           "message": "The specified location is not in a deliverable area.", "kind": "error",
           "metadata": {"details": "The dropoff location is outside the delivery radius of the "
                                   "pickup location (Max Radius: 10.00 miles, Calculated "
                                   "Distance: 23.71 miles)."}}


def refused() -> IntegrationError:
    return IntegrationError("uber", "POST .../delivery_quotes returned 400", status_code=400,
                            payload=REFUSAL)


@pytest.fixture
def zip_town(monkeypatch):
    async def lookup(postal_code, country=None):
        return GeoPoint(latitude=40.55, longitude=-74.46, postal_code=postal_code,
                        city="Piscataway", state="New Jersey", state_code="NJ")
    monkeypatch.setattr(delivery_address, "geocode_postal_code", lookup)


class Asks(list):
    """Each quote Uber was asked for; `answers` holds what to raise, in turn."""

    def __init__(self):
        super().__init__()
        self.answers: list = []


@pytest.fixture
def quotes(monkeypatch):
    calls = Asks()

    async def get_quote(**kw):
        calls.append(kw)
        problem = calls.answers.pop(0) if calls.answers else None
        if problem is not None:
            raise problem
        return uber.DeliveryQuote(quote_id="dqt_1", fee=Decimal("6.49"))

    monkeypatch.setattr(uber.uber_direct, "get_quote", get_quote)
    return calls


# --- one address for Uber ---------------------------------------------------------------
async def test_the_uber_address_is_the_customers_own_town(outlet, zip_town):
    address = await delivery_address.uber_dropoff(street="12 Maple St", unit="2B",
                                                  postal_code="08854", kitchen=outlet)
    assert address == {"street_address": ["12 Maple St", "2B"], "city": "Piscataway",
                       "state": "NJ", "zip_code": "08854", "country": "US"}


async def test_the_kitchens_town_is_only_the_last_resort(outlet):
    address = await delivery_address.uber_dropoff(street="12 Maple St", unit=None,
                                                  postal_code="08820", kitchen=outlet)
    assert (address["city"], address["state"], address["street_address"]) == (
        "Edison", "NJ", ["12 Maple St"])


# --- the quote --------------------------------------------------------------------------
async def test_the_quote_is_for_the_bookings_own_window(outlet, quotes):
    start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
    quote, why = await pricing._quote_delivery(
        outlet=outlet, dropoff_address={"street_address": ["1 Wood Ave"]},
        dropoff_latitude=40.52, dropoff_longitude=-74.41,
        slot_starts_at=start, slot_ends_at=start + timedelta(hours=1))
    assert (quote.fee, why) == (Decimal("6.49"), "")
    [call] = quotes
    before = timedelta(hours=settings.uber_dispatch_hours_before)
    assert (call["pickup_ready_at"], call["pickup_deadline_at"]) == (start - before,
                                                                    start + timedelta(hours=1))
    assert (call["dropoff_ready_at"], call["dropoff_deadline_at"]) == (start,
                                                                      start + timedelta(hours=1))


async def test_beyond_ubers_radius_is_undeliverable_not_down(outlet, quotes):
    quotes.answers.append(refused())
    quote, why = await pricing._quote_delivery(
        outlet=outlet, dropoff_address={}, dropoff_latitude=None, dropoff_longitude=None,
        slot_starts_at=NOW, slot_ends_at=NOW + timedelta(hours=1))
    assert (quote, why) == (None, pricing.UNDELIVERABLE) and len(quotes) == 1


async def test_a_window_uber_refuses_is_quoted_without_it(outlet, quotes):
    quotes.answers.append(IntegrationError("uber", "400 invalid_params", status_code=400,
                                           payload={"code": "invalid_params"}))
    start = datetime.now(timezone.utc) + timedelta(days=1)
    quote, why = await pricing._quote_delivery(
        outlet=outlet, dropoff_address={}, dropoff_latitude=None, dropoff_longitude=None,
        slot_starts_at=start, slot_ends_at=start + timedelta(hours=1))
    assert quote is not None and why == ""
    assert set(quotes[1]) - {"pickup", "dropoff", "pickup_latitude", "pickup_longitude",
                             "dropoff_latitude", "dropoff_longitude"} == {"pickup_ready_at"}


async def test_uber_down_is_unavailable(outlet, quotes):
    quotes.answers.append(IntegrationError("uber", "503", status_code=503))
    _, why = await pricing._quote_delivery(
        outlet=outlet, dropoff_address={}, dropoff_latitude=None, dropoff_longitude=None,
        slot_starts_at=NOW, slot_ends_at=NOW + timedelta(hours=1))
    assert why == pricing.UNAVAILABLE


async def test_no_price_is_never_a_zero_delivery_charge(session, outlet, quotes, monkeypatch):
    monkeypatch.setattr(settings, "delivery_fee_fallback", 4.0)       # even with a dev fallback
    monkeypatch.setattr(settings, "app_env", "development")

    async def one_line(session_, items):
        return [pricing.PricedLine("x", "Sambar", 1, Decimal("10.00"), Decimal("10.00"))]

    monkeypatch.setattr(pricing, "_price_lines", one_line)
    quotes.answers.append(refused())
    priced = await pricing.price_cart(session, [{"retailer_id": "x", "quantity": 1}],
                                      outlet=outlet, dropoff_address={})
    assert priced.delivery_quote_failed and priced.delivery_quote_error == pricing.UNDELIVERABLE


# --- the ordering page --------------------------------------------------------------------
@pytest.fixture
def page(session, customer, outlet, quotes, monkeypatch):
    monkeypatch.setattr(settings, "admin_session_secret", "test-secret")
    monkeypatch.setattr(order_web, "_clean_cart", lambda lines: [{"retailer_id": "x",
                                                                  "quantity": 1}])

    async def one_line(session_, items):
        return [pricing.PricedLine("x", "Sambar", 1, Decimal("10.00"), Decimal("10.00"))]

    monkeypatch.setattr(pricing, "_price_lines", one_line)
    with use_sender(Recorder()):
        yield order_link.build_token(customer.id)


async def _near_and_slot(session, customer, token):
    near = await addresses.save(session, customer, {**NEAR, "apartment_unit": "4C"})
    checked = await order_web.check_address(FakeRequest({"address_id": str(near.id)}), token,
                                            session=session)
    assert checked["ok"] is True
    return {"address_id": str(near.id), "slot_id": checked["slots"][0]["id"]}


@pytest.mark.parametrize("step", ["quote", "confirm"])
async def test_an_address_uber_refuses_never_reaches_payment(session, customer, page, quotes,
                                                            step):
    body = await _near_and_slot(session, customer, page)          # inside our own radius
    quotes.answers.append(refused())
    data = await getattr(order_web, step)(FakeRequest(body), page, session=session)
    assert data["ok"] is False and data["error"] == order_web.OUT_OF_RANGE
    assert (await session.execute(select(func.count(Order.id)))).scalar_one() == 0


async def test_the_page_quotes_uber_the_full_address(session, customer, page, quotes):
    body = await _near_and_slot(session, customer, page)
    data = await order_web.quote(FakeRequest(body), page, session=session)
    assert data["ok"] is True and data["delivery_fee"] == 6.49
    sent = quotes[-1]["dropoff"]
    assert sent == {"street_address": ["1 Wood Ave", "4C"], "city": "Edison", "state": "NJ",
                    "zip_code": "08830", "country": "US"}
    assert quotes[-1]["dropoff_deadline_at"] is not None


# --- the booking --------------------------------------------------------------------------
async def test_a_booking_uber_refuses_for_good_is_not_retried(session, customer, outlet,
                                                              monkeypatch):
    order = await _order(session, customer, outlet, starts_in=timedelta(hours=1))

    async def refuse(**_kw):
        raise IntegrationError("uber", "POST .../deliveries returned 400", status_code=400,
                               payload=REFUSAL)

    monkeypatch.setattr(dispatch.uber, "create_delivery", refuse)
    assert await dispatch.dispatch(session, order, now=NOW) is False
    assert order.uber_retry_stopped_at == NOW
    assert order.uber_dispatch_error.startswith("Uber can't deliver to this address: ")
    assert "23.71 miles" in order.uber_dispatch_error
    assert order not in await dispatch.due_orders(session, NOW)          # no more automatic tries
    [row] = [r for r in await dispatch.queue(session, NOW) if r.order is order]
    assert row.status == dispatch.NEEDS_ATTENTION


async def test_send_now_after_a_fix_books_it(session, customer, outlet, monkeypatch):
    order = await _order(session, customer, outlet, starts_in=timedelta(hours=1))
    order.uber_retry_stopped_at, order.uber_dispatch_error = NOW, "Uber can't deliver..."

    async def book(**kw):
        return uber.Delivery(delivery_id="del_9", status="pending", fee=Decimal("8.25"))

    monkeypatch.setattr(dispatch.uber, "create_delivery", book)
    order.delivery_fee = Decimal("6.49")
    assert await dispatch.dispatch(session, order, now=NOW)
    assert order.uber_retry_stopped_at is None and order.uber_dispatch_error is None
    assert (order.delivery_fee, order.uber_fee) == (Decimal("6.49"), Decimal("8.25"))


async def test_the_booking_sends_the_quotes_address(session, customer, outlet, zip_town,
                                                    monkeypatch):
    order = await _order(session, customer, outlet, starts_in=timedelta(hours=2))
    calls = []

    async def book(**kw):
        calls.append(kw)
        return uber.Delivery(delivery_id="del_1")

    monkeypatch.setattr(dispatch.uber, "create_delivery", book)
    await dispatch.dispatch(session, order, now=NOW)
    quoted = await delivery_address.uber_dropoff(street=order.delivery_address,
                                                 unit=order.apartment_unit,
                                                 postal_code=order.postal_code, kitchen=outlet)
    assert calls[0]["dropoff"] == quoted
    assert quoted["city"] == "Piscataway"                          # not the kitchen's Edison
