"""Shiprocket's API, for its Hyperlocal (Shiprocket Quick) riders.

Booked from Mark ready (services/shiprocket_booking.py): an order is created
with shipping_method HL and the drop's map pin, then a rider is assigned to
its shipment (Generate AWB). Cancel booking cancels the Shiprocket order.
https://apidocs.shiprocket.in, Hyperlocal. Shiprocket has no test mode:
every call is on the live account.

The API signs in with an API user (Shiprocket > Settings > API Users): its
email and password give a token that lasts 10 days. It is kept per business
and sign-in, and fetched again on a 401.

The account's single tracking webhook belongs to the website
(sherohomefood.com), so nothing here relies on one: a shipment's tracking is
asked for when staff check on the rider.
"""

from __future__ import annotations

import time

import httpx

from app.core import tenants
from app.core.config import settings
from app.core.exceptions import IntegrationError
from app.core.logging import get_logger

log = get_logger(__name__)

TIMEOUT = httpx.Timeout(30.0, connect=10.0)
# Shiprocket's tokens last 10 days; one is fetched again a day early.
TOKEN_SECONDS = 9 * 24 * 3600
# business key + API user -> (token, monotonic expiry)
_tokens: dict[str, tuple[str, float]] = {}


class ShiprocketError(IntegrationError):
    def __init__(self, message: str, *, status_code: int | None = None,
                 payload: object | None = None) -> None:
        super().__init__("shiprocket", message, status_code=status_code, payload=payload)


def configured() -> bool:
    """Whether this business has a Shiprocket API user."""
    return bool(settings.shiprocket_email.strip() and settings.shiprocket_password)


def _url(path: str) -> str:
    return settings.shiprocket_base_url.rstrip("/") + "/" + path.lstrip("/")


def _key() -> str:
    return f"{tenants.current().key}:{settings.shiprocket_email.strip().lower()}"


def message_of(response: httpx.Response) -> str:
    """Shiprocket's own words for a refusal, short."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict):
        parts = [str(body.get("message") or "")]
        errors = body.get("errors")
        if isinstance(errors, dict):
            parts += [f"{k}: {' '.join(map(str, v)) if isinstance(v, list) else v}"
                      for k, v in errors.items()]
        return " ".join(p for p in parts if p)[:300] or response.text[:300]
    return str(body)[:300]


async def _token(client: httpx.AsyncClient) -> str:
    cached = _tokens.get(_key())
    if cached and cached[1] > time.monotonic():
        return cached[0]
    response = await client.post(_url("auth/login"), json={
        "email": settings.shiprocket_email.strip(), "password": settings.shiprocket_password})
    token = None
    if response.status_code < 300:
        try:
            token = response.json().get("token")
        except ValueError:
            token = None
    if not token:
        raise ShiprocketError(
            f"Shiprocket sign-in failed ({response.status_code}): {message_of(response)}. "
            "Check DST_SHIPROCKET_EMAIL and DST_SHIPROCKET_PASSWORD (an API user).",
            status_code=response.status_code)
    _tokens[_key()] = (token, time.monotonic() + TOKEN_SECONDS)
    log.info("shiprocket_signed_in", business=tenants.current().key)
    return token


async def request(method: str, path: str, *, params: dict | None = None,
                  json: dict | None = None):
    """One API call, signed in; a 401 signs in again once."""
    if not configured():
        raise ShiprocketError("Shiprocket is not connected: set DST_SHIPROCKET_EMAIL and "
                              "DST_SHIPROCKET_PASSWORD (an API user) on the server.")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            for attempt in (1, 2):
                token = await _token(client)
                response = await client.request(method, _url(path), params=params, json=json,
                                                headers={"Authorization": f"Bearer {token}"})
                if response.status_code == 401 and attempt == 1:
                    _tokens.pop(_key(), None)
                    continue
                break
    except httpx.HTTPError as exc:
        raise ShiprocketError(f"Shiprocket not reachable: {exc.__class__.__name__}") from exc
    if response.status_code >= 300:
        raise ShiprocketError(f"Shiprocket answered {response.status_code}: {message_of(response)}",
                              status_code=response.status_code)
    try:
        return response.json()
    except ValueError:
        return {}


# --- the calls -------------------------------------------------------------------------------
async def pickup_locations() -> list[str]:
    """The pickup location names on the account (Settings > Pickup Addresses)."""
    data = await request("GET", "settings/company/pickup")
    rows = ((data or {}).get("data") or {}).get("shipping_address") or []
    return [str(r["pickup_location"]) for r in rows if isinstance(r, dict) and r.get("pickup_location")]


async def hyperlocal_rates(*, pickup_postcode: str, delivery_postcode: str,
                           lat_from: float, long_from: float, lat_to: float, long_to: float,
                           weight: float) -> list[dict]:
    """The riders that can take this drop, cheapest first (couriers())."""
    data = await request("GET", "courier/serviceability/", params={
        "pickup_postcode": pickup_postcode, "delivery_postcode": delivery_postcode,
        "cod": 0, "weight": weight, "is_new_hyperlocal": 1,
        "lat_from": lat_from, "long_from": long_from, "lat_to": lat_to, "long_to": long_to,
    })
    return couriers(data)


async def create_order(payload: dict) -> dict:
    """POST orders/create/adhoc: {order_id, shipment_id, status, ...}."""
    data = await request("POST", "orders/create/adhoc", json=payload)
    return data if isinstance(data, dict) else {}


async def assign_rider(shipment_id: int | str, courier_id: int | None = None) -> dict:
    """POST courier/assign/awb: a rider (courier) for the shipment."""
    body: dict = {"shipment_id": int(shipment_id)}
    if courier_id:
        body["courier_id"] = int(courier_id)
    data = await request("POST", "courier/assign/awb", json=body)
    return data if isinstance(data, dict) else {}


async def cancel_order(order_id: int | str) -> dict:
    """POST orders/cancel: cancel a Shiprocket order, with its shipment and
    any rider assigned to it. Answers {"status_code": 200, "message": ...}."""
    data = await request("POST", "orders/cancel", json={"ids": [int(order_id)]})
    return data if isinstance(data, dict) else {}


async def track_shipment(shipment_id: int | str) -> dict:
    data = await request("GET", f"courier/track/shipment/{shipment_id}")
    if isinstance(data, list):
        data = data[0] if data else {}
    return data if isinstance(data, dict) else {}


# --- reading the answers ---------------------------------------------------------------------
def couriers(data) -> list[dict]:
    """The couriers a serviceability answer offers, cheapest first, as
    {id, name, rate}. The Hyperlocal answer is data: [...]; the standard one
    data.available_courier_companies. Shiprocket Quick (hyperlocal) comes
    without an id: its rider is then assigned without naming a courier."""
    raw = data.get("data") if isinstance(data, dict) else None
    if isinstance(raw, dict):
        raw = raw.get("available_courier_companies") or []
    options = []
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, dict):
            continue
        ident = row.get("courier_company_id") or row.get("courier_id") or row.get("id")
        try:
            rate = float(row.get("rates") if row.get("rates") is not None
                         else row.get("rate") if row.get("rate") is not None
                         else row.get("freight_charge"))
        except (TypeError, ValueError):
            rate = None
        options.append({
            "id": int(ident) if str(ident or "").isdigit() else None,
            "name": str(row.get("courier_name") or ""),
            "rate": rate,
        })
    return sorted(options, key=lambda c: (c["rate"] is None, c["rate"] or 0))


def assignment(data: dict) -> dict:
    """What Generate AWB answered: {awb, courier, error}. A refusal comes
    back as HTTP 200 with awb_assign_status 0; a Hyperlocal booking may only
    say it is being processed (no AWB yet)."""
    response = data.get("response") if isinstance(data.get("response"), dict) else {}
    details = response.get("data") if isinstance(response.get("data"), dict) else {}
    error = details.get("awb_assign_error") or (
        data.get("message") if data.get("awb_assign_status") == 0 or data.get("success") is False
        else None)
    return {
        "awb": str(details.get("awb_code") or "") or None,
        "courier": str(details.get("courier_name") or "") or None,
        "error": str(error) if error else None,
    }


def tracking(data: dict) -> dict:
    """A shipment's tracking, as {status, awb, courier, url, rider, eta, error}."""
    info = data.get("tracking_data") if isinstance(data.get("tracking_data"), dict) else {}
    track = (info.get("shipment_track") or [{}])[0] if isinstance(info.get("shipment_track"), list) else {}
    track = track if isinstance(track, dict) else {}
    agent = track.get("courier_agent_details")
    rider = None
    if isinstance(agent, dict):
        rider = " ".join(str(agent.get(k) or "") for k in ("name", "mobile", "phone")).strip() or None
    elif agent:
        rider = str(agent)[:120]
    return {
        "status": str(track.get("current_status") or "") or None,
        "awb": str(track.get("awb_code") or "") or None,
        "courier": str(track.get("courier_name") or "") or None,
        "url": str(info.get("track_url") or "") or None,
        "rider": rider,
        "eta": str(info.get("etd") or track.get("edd") or "") or None,
        "error": str(info.get("error") or "") or None,
    }
