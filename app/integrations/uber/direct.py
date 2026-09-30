"""Uber Direct delivery quote (spec step 14).

Supplies the delivery charge and any extra taxes/fees shown on the order
summary and charged through Stripe.

Amounts come back in minor units (cents) and are converted to Decimal here so
no float ever touches a money value.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.core.config import settings
from app.core.exceptions import IntegrationError
from app.core.logging import get_logger
from app.integrations.base import ApiClient
from app.integrations.uber.oauth import get_access_token, invalidate_token

log = get_logger(__name__)


@dataclass(frozen=True)
class DeliveryQuote:
    """Normalised quote. `fee` and `extra_fees` are separate spec line items."""

    quote_id: str
    fee: Decimal
    extra_fees: Decimal = Decimal("0.00")
    currency: str = "USD"
    eta_minutes: int | None = None
    expires_at: datetime | None = None
    raw: dict = field(default_factory=dict)

    @property
    def total(self) -> Decimal:
        return self.fee + self.extra_fees


class UberDirectClient(ApiClient):
    service = "uber"

    def __init__(self) -> None:
        super().__init__(base_url=settings.uber_api_base_url)

    async def default_headers(self) -> dict[str, str]:
        token = await get_access_token()
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def request(self, method: str, url: str, **kw):
        try:
            return await super().request(method, url, **kw)
        except IntegrationError as exc:
            if exc.status_code != 401:
                raise
            invalidate_token()
            await get_access_token(force_refresh=True)
            return await super().request(method, url, **kw)

    async def get_quote(
        self,
        *,
        pickup: dict,
        dropoff: dict,
        pickup_latitude: float,
        pickup_longitude: float,
        dropoff_latitude: float | None = None,
        dropoff_longitude: float | None = None,
        pickup_ready_at: datetime | None = None,
        dropoff_deadline_at: datetime | None = None,
    ) -> DeliveryQuote:
        """Quote a delivery from an outlet to the customer.

        `pickup` / `dropoff` are structured address dicts (see `build_address`).
        Coordinates are sent alongside because a ZIP-only dropoff geocodes
        poorly on Uber's side.
        """
        body: dict = {
            "pickup_address": json.dumps(pickup),
            "dropoff_address": json.dumps(dropoff),
            "pickup_latitude": pickup_latitude,
            "pickup_longitude": pickup_longitude,
        }
        if dropoff_latitude is not None and dropoff_longitude is not None:
            body["dropoff_latitude"] = dropoff_latitude
            body["dropoff_longitude"] = dropoff_longitude
        if pickup_ready_at:
            body["pickup_ready_dt"] = _iso(pickup_ready_at)
        if dropoff_deadline_at:
            body["dropoff_deadline_dt"] = _iso(dropoff_deadline_at)

        payload = await self.post(
            f"/v1/customers/{settings.uber_customer_id}/delivery_quotes", json=body
        )
        return _parse_quote(payload or {})


@dataclass(frozen=True)
class Delivery:
    """A booked Uber Direct delivery."""

    delivery_id: str
    status: str = ""
    tracking_url: str | None = None
    fee: Decimal = Decimal("0.00")
    raw: dict = field(default_factory=dict)


async def create_delivery(*, pickup: dict, pickup_name: str, pickup_phone: str,
                          pickup_latitude: float, pickup_longitude: float,
                          dropoff: dict, dropoff_name: str, dropoff_phone: str,
                          dropoff_latitude: float | None, dropoff_longitude: float | None,
                          items: list[dict], external_id: str,
                          pickup_ready_at: datetime, pickup_deadline_at: datetime,
                          dropoff_ready_at: datetime, dropoff_deadline_at: datetime,
                          dropoff_notes: str | None = None) -> Delivery:
    """Book a courier (Uber Direct `POST /deliveries`).

    `items` is Uber's manifest: [{"name", "quantity", "size"}]. The four
    times bound when the courier may collect and when they must deliver.
    """
    body: dict = {
        "pickup_name": pickup_name,
        "pickup_address": json.dumps(pickup),
        "pickup_phone_number": pickup_phone,
        "pickup_latitude": pickup_latitude,
        "pickup_longitude": pickup_longitude,
        "dropoff_name": dropoff_name,
        "dropoff_address": json.dumps(dropoff),
        "dropoff_phone_number": dropoff_phone,
        "manifest_items": items,
        "external_id": external_id,
        "pickup_ready_dt": _iso(pickup_ready_at),
        "pickup_deadline_dt": _iso(pickup_deadline_at),
        "dropoff_ready_dt": _iso(dropoff_ready_at),
        "dropoff_deadline_dt": _iso(dropoff_deadline_at),
    }
    if dropoff_latitude is not None and dropoff_longitude is not None:
        body["dropoff_latitude"] = dropoff_latitude
        body["dropoff_longitude"] = dropoff_longitude
    if dropoff_notes:
        body["dropoff_notes"] = dropoff_notes[:280]
    payload = await uber_direct.post(
        f"/v1/customers/{settings.uber_customer_id}/deliveries", json=body) or {}
    delivery_id = str(payload.get("id") or "")
    if not delivery_id:
        raise IntegrationError("uber", "delivery created without an id", payload=payload)
    return Delivery(delivery_id=delivery_id, status=str(payload.get("status") or ""),
                    tracking_url=payload.get("tracking_url"),
                    fee=minor_to_decimal(payload.get("fee")), raw=payload)


async def cancel_delivery(delivery_id: str) -> str:
    """Cancel a booked delivery; returns Uber's new status."""
    payload = await uber_direct.post(
        f"/v1/customers/{settings.uber_customer_id}/deliveries/{delivery_id}/cancel",
        json={}) or {}
    return str(payload.get("status") or "")


def build_address(*, street: str, city: str, state: str, zip_code: str,
                  country: str = "US") -> dict:
    """Uber's address object. `street_address` is a list of up to two lines."""
    return {
        "street_address": [street],
        "city": city,
        "state": state,
        "zip_code": zip_code,
        "country": country,
    }


def minor_to_decimal(value: object) -> Decimal:
    """Convert cents to a 2dp Decimal, tolerating None or a string."""
    if value in (None, ""):
        return Decimal("0.00")
    try:
        return (Decimal(str(value)) / Decimal("100")).quantize(Decimal("0.01"))
    except Exception:  # noqa: BLE001 - any malformed amount is treated as zero
        log.warning("uber_unparsable_amount", value=repr(value))
        return Decimal("0.00")


def _parse_quote(payload: dict) -> DeliveryQuote:
    expires_raw = payload.get("expires")
    return DeliveryQuote(
        quote_id=str(payload.get("id") or payload.get("kind") or ""),
        fee=minor_to_decimal(payload.get("fee")),
        extra_fees=minor_to_decimal(payload.get("tax")),
        currency=(payload.get("currency") or settings.stripe_currency).upper(),
        eta_minutes=payload.get("duration"),
        expires_at=_parse_dt(expires_raw),
        raw=payload,
    )


def _parse_dt(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(value: datetime) -> str:
    return value.astimezone().isoformat()


uber_direct = UberDirectClient()
