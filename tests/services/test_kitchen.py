"""Single-kitchen serviceability (spec steps 6a, 9, 10)."""

import pytest

from app.integrations.geo.geocoder import GeoPoint
from app.services.kitchen import check_service, get_kitchen, resolve_location

# The `outlet` fixture sits at 40.5187, -74.4121 with a 12 km radius.
NEAR = GeoPoint(latitude=40.5300, longitude=-74.4000, postal_code="08820")
FAR = GeoPoint(latitude=39.9526, longitude=-75.1652, postal_code="19103")


async def test_no_kitchen_configured_is_not_serviceable(session):
    check = await check_service(session, NEAR)
    assert check.is_serviceable is False
    assert "admin dashboard" in check.reason


async def test_primary_kitchen_wins_over_an_older_one(session, far_outlet, outlet):
    """`outlet` is flagged primary; `far_outlet` is older but is not."""
    kitchen = await get_kitchen(session)
    assert kitchen is not None
    assert kitchen.code == outlet.code


async def test_inactive_kitchen_is_skipped(session, outlet, far_outlet):
    outlet.is_active = False
    await session.flush()
    kitchen = await get_kitchen(session)
    assert kitchen.code == "JERSEYCITY"


# --- radius mode -------------------------------------------------------------
async def test_radius_mode_accepts_a_nearby_point(session, outlet):
    check = await check_service(session, NEAR)
    assert check.is_serviceable is True
    assert check.distance_km is not None and check.distance_km < 12
    assert check.kitchen_name == "Shero Edison"


async def test_radius_mode_rejects_a_distant_point(session, outlet):
    check = await check_service(session, FAR)
    assert check.is_serviceable is False
    assert "beyond the" in check.reason


async def test_radius_mode_fails_closed_without_coordinates(session, outlet):
    """No coordinates means no decision, and no decision means no delivery."""
    check = await check_service(session, None, postal_code="08820")
    assert check.is_serviceable is False


# --- ZIP mode ----------------------------------------------------------------
async def test_zip_mode_accepts_a_listed_code(session, outlet):
    outlet.service_area_mode = "zips"
    outlet.service_zips = ["08820", "08817"]
    await session.flush()

    check = await check_service(session, FAR, postal_code="08820")
    # Distance is irrelevant in ZIP mode - the list is the rule.
    assert check.is_serviceable is True


async def test_zip_mode_rejects_an_unlisted_code(session, outlet):
    outlet.service_area_mode = "zips"
    outlet.service_zips = ["21075"]
    await session.flush()

    check = await check_service(session, NEAR, postal_code="08820")
    assert check.is_serviceable is False
    assert "ZIP list" in check.reason


@pytest.mark.parametrize("supplied", ["08820", " 08820 ", "08-820", "08820"])
async def test_zip_matching_ignores_spacing_and_dashes(session, outlet, supplied):
    outlet.service_area_mode = "zips"
    outlet.service_zips = ["08820"]
    await session.flush()

    check = await check_service(session, NEAR, postal_code=supplied)
    assert check.is_serviceable is True


async def test_empty_zip_list_means_no_zip_restriction(session, outlet):
    outlet.service_area_mode = "zips"
    outlet.service_zips = []
    await session.flush()

    check = await check_service(session, FAR, postal_code="99999")
    assert check.is_serviceable is True


# --- both mode ---------------------------------------------------------------
async def test_both_mode_needs_the_radius_and_the_zip(session, outlet):
    outlet.service_area_mode = "both"
    outlet.service_zips = ["08820"]
    await session.flush()

    assert (await check_service(session, NEAR, postal_code="08820")).is_serviceable
    # Right ZIP, too far.
    assert not (await check_service(session, FAR, postal_code="08820")).is_serviceable
    # Close enough, wrong ZIP.
    assert not (await check_service(session, NEAR, postal_code="99999")).is_serviceable


# --- location resolution -----------------------------------------------------
async def test_a_pin_beats_a_typed_zip():
    point = await resolve_location(latitude=40.5, longitude=-74.4,
                                   postal_code="99999")
    assert point is not None
    assert point.source == "pin"
    assert point.latitude == 40.5


async def test_nothing_supplied_resolves_to_none():
    assert await resolve_location() is None
