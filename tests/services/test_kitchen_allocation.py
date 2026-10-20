"""Capacity-aware partner allocation using the real slot and order services."""
import pytest
from sqlalchemy import select

from app.core.exceptions import NoSlotsAvailableError
from app.db.models import DeliverySlot
from app.integrations.geo.geocoder import GeoPoint
from app.services import kitchen, orders, slots
from tests.services.test_orders import _priced


async def allocate(session, customer):
    return await kitchen.check_service(
        session, GeoPoint(latitude=customer.latitude, longitude=customer.longitude),
        postal_code=customer.postal_code, require_capacity=True)


async def fill(session, outlet):
    await slots.list_available_slots(session, outlet)
    for slot in (await session.execute(select(DeliverySlot).where(
            DeliverySlot.outlet_id == outlet.id))).scalars():
        slot.reserved_count = slot.capacity
    await session.flush()


async def test_nearest_partner_with_capacity_is_selected(session, customer, outlet, far_outlet):
    far_outlet.delivery_radius_km = 100
    far_outlet.operating_hours = outlet.operating_hours
    assert (await allocate(session, customer)).kitchen.id == outlet.id
    await fill(session, outlet)
    assert (await allocate(session, customer)).kitchen.id == far_outlet.id


async def test_all_full_is_temporary_unavailability(session, customer, outlet):
    await fill(session, outlet)
    result = await allocate(session, customer)
    assert not result.is_serviceable and result.kitchen is None
    assert not result.kitchen_name  # no out-of-area notification
    assert "no available delivery slots" in result.reason


async def test_no_hours_skips_partner(session, customer, outlet, far_outlet):
    outlet.operating_hours = {}
    far_outlet.delivery_radius_km = 100
    far_outlet.operating_hours = {d: [["11:00", "21:00"]] for d in slots.WEEKDAY_KEYS}
    assert (await allocate(session, customer)).kitchen.id == far_outlet.id


async def test_paused_and_outside_area_never_get_fallback(session, customer, outlet, far_outlet):
    await fill(session, outlet)
    far_outlet.delivery_radius_km = 100
    far_outlet.is_active = False
    assert (await allocate(session, customer)).kitchen is None
    far_outlet.is_active = True
    far_outlet.delivery_radius_km = 1
    assert (await allocate(session, customer)).kitchen is None


async def test_reservation_uses_last_capacity_then_reallocates(session, customer, outlet, far_outlet):
    far_outlet.delivery_radius_km = 100
    far_outlet.operating_hours = outlet.operating_hours
    await fill(session, outlet)
    options = list((await session.execute(select(DeliverySlot).where(
        DeliverySlot.outlet_id == outlet.id,
        DeliverySlot.starts_at >= slots.earliest_bookable()))).scalars())
    target = options[0]
    target.reserved_count = target.capacity - 1
    await session.flush()
    assert (await allocate(session, customer)).kitchen.id == outlet.id
    order = await orders.create_draft_order(session, customer=customer, outlet=outlet,
                                           priced=_priced(), slot_id=target.id)
    await slots.hold_slot(session, slot_id=target.id, order_id=order.id)
    assert order.outlet_id == outlet.id
    assert (await allocate(session, customer)).kitchen.id == far_outlet.id
    await slots.release_holds_for_order(session, order.id, reason="cancelled")
    assert (await allocate(session, customer)).kitchen.id == outlet.id


async def test_checkout_refuses_paused_partner_and_foreign_slot(session, customer, outlet, far_outlet):
    option = (await slots.list_available_slots(session, outlet))[0]
    with pytest.raises(NoSlotsAvailableError):
        await orders.create_draft_order(session, customer=customer, outlet=far_outlet,
                                       priced=_priced(), slot_id=option.slot_id)
    outlet.is_active = False
    with pytest.raises(NoSlotsAvailableError):
        await orders.create_draft_order(session, customer=customer, outlet=outlet,
                                       priced=_priced(), slot_id=option.slot_id)


async def test_hold_refuses_partner_paused_after_draft(session, customer, outlet):
    option = (await slots.list_available_slots(session, outlet))[0]
    order = await orders.create_draft_order(session, customer=customer, outlet=outlet,
                                           priced=_priced(), slot_id=option.slot_id)
    outlet.is_active = False
    await session.flush()
    with pytest.raises(NoSlotsAvailableError):
        await slots.hold_slot(session, slot_id=option.slot_id, order_id=order.id)


async def test_web_address_flow_uses_capacity_fallback(session, customer, outlet, far_outlet):
    from app.api.routes import order_web

    far_outlet.delivery_radius_km = 100
    far_outlet.operating_hours = outlet.operating_hours
    await fill(session, outlet)
    result, point = await order_web._serving(session, customer)
    assert result.kitchen.id == far_outlet.id
    assert point.latitude == customer.latitude


async def test_web_capacity_refusal_does_not_send_out_of_area_alert(session, customer, outlet, monkeypatch):
    from app.api.routes import order_web

    async def unexpected(*args, **kwargs):
        raise AssertionError("capacity refusal must not send an out-of-area notification")

    monkeypatch.setattr(order_web.out_of_area, "notify", unexpected)
    await fill(session, outlet)
    result, _ = await order_web._serving(session, customer)
    response = await order_web._refuse(customer, customer, result)
    assert response["ok"] is False
    assert "no available delivery slots" in response["error"]
    assert "out_of_area" not in response
