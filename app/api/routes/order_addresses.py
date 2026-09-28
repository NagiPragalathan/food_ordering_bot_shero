"""Saved addresses and the map pin lookup for the web ordering page.

Kept apart from `order_web.py`, which is the ordering flow itself. Same
token rule: every route re-reads the signed link, and an address is only
ever looked up among the link owner's own.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import get_session
from app.integrations.geo.geocoder import geocode_address, reverse_geocode
from app.services import addresses, crm_sync, order_link

log = get_logger(__name__)
router = APIRouter(prefix="/order", tags=["ordering"])



@router.get("/{token}/addresses")
async def list_addresses(token: str,
                         session: AsyncSession = Depends(get_session)) -> dict:
    customer = await order_link.customer_for_token(session, token)
    if customer is None:
        return order_link.dead_link_response()

    saved = await addresses.list_for(session, customer)
    return {
        "ok": True,
        "addresses": [addresses.as_dict(a) for a in saved],
        # Shown on the page, never editable there: the driver calls the
        # number the customer is chatting from.
        "contact_number": customer.whatsapp_number,
        "labels": list(addresses.SUGGESTED_LABELS),
    }


@router.post("/{token}/addresses")
async def save_address(request: Request, token: str,
                       session: AsyncSession = Depends(get_session)) -> dict:
    """Add an address, or edit one when the body carries its `id`."""
    customer = await order_link.customer_for_token(session, token)
    if customer is None:
        return order_link.dead_link_response()

    body = await _json(request)
    try:
        address = await addresses.save(session, customer, body,
                                       address_id=body.get("id") or None)
    except addresses.AddressError as exc:
        return {"ok": False, "error": str(exc)}
    # The Lead gets the address as soon as it is saved, not only at checkout.
    await crm_sync.push_details(customer, **addresses.crm_details(address))
    return {"ok": True, "address": addresses.as_dict(address)}


@router.post("/{token}/addresses/{address_id}/delete")
async def delete_address(token: str, address_id: str,
                         session: AsyncSession = Depends(get_session)) -> dict:
    customer = await order_link.customer_for_token(session, token)
    if customer is None:
        return order_link.dead_link_response()

    try:
        await addresses.delete(session, customer, address_id)
    except addresses.AddressError as exc:
        return {"ok": False, "error": str(exc)}
    saved = await addresses.list_for(session, customer)
    return {"ok": True, "addresses": [addresses.as_dict(a) for a in saved]}


@router.get("/{token}/locate")
async def locate(token: str, lat: float, lng: float,
                 session: AsyncSession = Depends(get_session)) -> dict:
    """Street and ZIP at a map pin, to pre-fill the form.

    Goes through the server rather than the browser calling a geocoder
    directly, so a Google key (when set) never reaches the page and the
    OpenStreetMap usage rules - one identified caller - are kept.
    """
    customer = await order_link.customer_for_token(session, token)
    if customer is None:
        return order_link.dead_link_response()

    point = await reverse_geocode(lat, lng)
    if point is None:
        return {"ok": False,
                "error": "We could not find a street there. Please type the address."}
    return {"ok": True, "address_line1": point.street,
            "postal_code": point.postal_code, "city": point.city,
            "formatted": point.formatted_address}


@router.get("/{token}/search")
async def search(token: str, q: str = "",
                 session: AsyncSession = Depends(get_session)) -> dict:
    """A typed address to a map point, for the search box above the map."""
    customer = await order_link.customer_for_token(session, token)
    if customer is None:
        return order_link.dead_link_response()

    point = await geocode_address(q)
    if point is None:
        return {"ok": False, "error": "We could not find that address. "
                                      "Try adding the town, or move the map."}
    return {"ok": True, "lat": point.latitude, "lng": point.longitude,
            "address_line1": point.street, "postal_code": point.postal_code,
            "formatted": point.formatted_address}


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
