"""Many kitchens: the customer's location is measured against every kitchen,
and the nearest one that delivers there gets the order (services/kitchen.py)."""

from __future__ import annotations

import pytest

from app.integrations.geo.geocoder import GeoPoint
from app.services import kitchen as kitchen_service

pytestmark = pytest.mark.asyncio

# `outlet` is Edison (12 km radius), `far_outlet` is Jersey City (10 km).
IN_EDISON = GeoPoint(latitude=40.5300, longitude=-74.4000, postal_code="08820")
IN_JERSEY_CITY = GeoPoint(latitude=40.7282, longitude=-74.0776, postal_code="07302")
PHILADELPHIA = GeoPoint(latitude=39.9526, longitude=-75.1652, postal_code="19103")


async def test_each_address_goes_to_the_kitchen_that_covers_it(session, outlet, far_outlet):
    edison = await kitchen_service.check_service(session, IN_EDISON)
    jersey = await kitchen_service.check_service(session, IN_JERSEY_CITY)

    assert edison.is_serviceable and edison.kitchen.code == "EDISON"
    assert jersey.is_serviceable and jersey.kitchen.code == "JERSEYCITY"
    assert jersey.distance_km < 10


async def test_where_two_kitchens_cover_it_the_nearest_wins(session, outlet, far_outlet):
    far_outlet.delivery_radius_km = 100.0          # now covers Edison too
    check = await kitchen_service.check_service(session, IN_EDISON)
    assert check.kitchen.code == "EDISON"


async def test_outside_every_kitchen_is_not_served_and_names_no_kitchen(
        session, outlet, far_outlet):
    check = await kitchen_service.check_service(session, PHILADELPHIA)
    assert check.is_serviceable is False and check.kitchen is None
    assert "beyond" in check.reason


async def test_a_switched_off_kitchen_is_never_matched(session, outlet, far_outlet):
    far_outlet.is_active = False
    await session.flush()
    check = await kitchen_service.check_service(session, IN_JERSEY_CITY)
    assert check.is_serviceable is False


async def test_the_customer_keeps_the_kitchen_picked_for_their_address(
        session, customer, outlet, far_outlet):
    customer.preferred_outlet_id = far_outlet.id
    assert (await kitchen_service.kitchen_for(session, customer)).code == "JERSEYCITY"

    # Switched off since: fall back to the default kitchen.
    far_outlet.is_active = False
    assert (await kitchen_service.kitchen_for(session, customer)).code == "EDISON"


def test_miles_and_km_round_trip():
    assert kitchen_service.km(10) == pytest.approx(16.093, abs=0.001)
    assert kitchen_service.miles(kitchen_service.km(10)) == 10
