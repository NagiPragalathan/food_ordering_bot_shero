"""A customer's saved delivery addresses (Home, Office, ...).

The web ordering page lists these, lets the customer add, edit and delete
them, and orders to one of them. Every lookup is scoped to the customer, so
an address id taken from somebody else's page is simply "not found".

Before this, a customer had exactly one address, stored on the `Customer`
row. That address is carried over the first time the list is read, so nobody
who already ordered has to type it again.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Customer, CustomerAddress

log = get_logger(__name__)

# One-tap choices on the page. The label is free text, so these are
# suggestions rather than an enum.
SUGGESTED_LABELS = ("Home", "Office", "Other")
# The label a carried-over address gets. We cannot know what it was.
CARRIED_OVER_LABEL = "Home"
MAX_ADDRESSES = 10

MAX_LABEL = 40
MAX_STREET = 255
MAX_APARTMENT = 120
MAX_ZIP = 20
MAX_INSTRUCTIONS = 500


class AddressError(ValueError):
    """A problem the customer can fix; the message is shown to them as-is."""


# --- reading -------------------------------------------------------------------
async def list_for(session: AsyncSession, customer: Customer) -> list[CustomerAddress]:
    """The customer's addresses, the default first, then most recently changed."""
    addresses = await _query(session, customer)
    if not addresses and customer.address_line1 and customer.postal_code:
        addresses = [await _carry_over(session, customer)]
    return addresses


async def get_owned(session: AsyncSession, customer: Customer,
                    address_id) -> CustomerAddress:
    """One of this customer's addresses, or AddressError.

    The same error for "no such id" and "somebody else's id", so the page
    cannot be used to probe which addresses exist.
    """
    try:
        wanted = uuid.UUID(str(address_id))
    except (TypeError, ValueError):
        raise AddressError("Choose a delivery address.") from None

    address = await session.get(CustomerAddress, wanted)
    if address is None or address.customer_id != customer.id:
        raise AddressError("That address could not be found. Please choose another.")
    return address


# --- writing -------------------------------------------------------------------
async def save(session: AsyncSession, customer: Customer, data: dict, *,
               address_id=None) -> CustomerAddress:
    """Create an address, or update one of the customer's own."""
    fields = clean(data)

    if address_id:
        address = await get_owned(session, customer, address_id)
    else:
        existing = await _query(session, customer)
        if len(existing) >= MAX_ADDRESSES:
            raise AddressError(f"You can save up to {MAX_ADDRESSES} addresses. "
                               "Delete one to add another.")
        address = CustomerAddress(customer_id=customer.id,
                                  is_default=not existing)
        session.add(address)

    for key, value in fields.items():
        setattr(address, key, value)
    await session.flush()
    log.info("address_saved", customer_id=str(customer.id), label=address.label,
             created=not address_id)
    return address


async def delete(session: AsyncSession, customer: Customer, address_id) -> None:
    address = await get_owned(session, customer, address_id)
    was_default = address.is_default
    await session.delete(address)
    await session.flush()

    # Keep exactly one default while any address is left.
    if was_default:
        remaining = await _query(session, customer)
        if remaining:
            remaining[0].is_default = True
            await session.flush()


async def use_for_order(session: AsyncSession, customer: Customer,
                        address: CustomerAddress) -> None:
    """Make this the delivery address: default next time, and on the customer.

    The WhatsApp flow, the CRM push and the delivery quote all read the
    address from the `Customer` row, so it is copied there. The contact
    number is always the customer's own WhatsApp number - the page shows it
    but does not let it be changed.
    """
    for other in await _query(session, customer):
        other.is_default = other.id == address.id

    customer.address_line1 = address.address_line1
    customer.apartment_unit = address.apartment_unit
    customer.postal_code = address.postal_code
    customer.delivery_instructions = address.delivery_instructions
    customer.contact_number = customer.whatsapp_number
    if address.latitude is not None and address.longitude is not None:
        customer.latitude, customer.longitude = address.latitude, address.longitude
    await session.flush()


# --- shaping -------------------------------------------------------------------
def clean(data: dict) -> dict:
    """Validate what the page sent. Raises AddressError with a friendly message."""
    label = _text(data.get("label"), MAX_LABEL) or "Other"
    street = _text(data.get("address_line1"), MAX_STREET)
    postal_code = _text(data.get("postal_code"), MAX_ZIP)
    if not street:
        raise AddressError("Enter your street address.")
    if not postal_code:
        raise AddressError("Enter your ZIP code.")

    latitude = _coordinate(data.get("latitude"), 90)
    longitude = _coordinate(data.get("longitude"), 180)
    # Half a pin is no pin: without both, fall back to the ZIP.
    if latitude is None or longitude is None:
        latitude = longitude = None

    return {
        "label": label,
        "address_line1": street,
        "apartment_unit": _text(data.get("apartment_unit"), MAX_APARTMENT) or None,
        "postal_code": postal_code,
        "delivery_instructions": (
            _text(data.get("delivery_instructions"), MAX_INSTRUCTIONS) or None),
        "latitude": latitude,
        "longitude": longitude,
    }


def crm_details(address: CustomerAddress) -> dict:
    """The address in crm_sync.push_details' terms."""
    return {"address": address.address_line1, "apartment_unit": address.apartment_unit,
            "postal_code": address.postal_code, "latitude": address.latitude,
            "longitude": address.longitude}


def as_dict(address: CustomerAddress) -> dict:
    return {
        "id": str(address.id),
        "label": address.label,
        "address_line1": address.address_line1,
        "apartment_unit": address.apartment_unit or "",
        "postal_code": address.postal_code,
        "delivery_instructions": address.delivery_instructions or "",
        "latitude": address.latitude,
        "longitude": address.longitude,
        "is_default": address.is_default,
    }


# --- helpers -------------------------------------------------------------------
async def _query(session: AsyncSession, customer: Customer) -> list[CustomerAddress]:
    result = await session.execute(
        select(CustomerAddress)
        .where(CustomerAddress.customer_id == customer.id)
        .order_by(CustomerAddress.is_default.desc(),
                  CustomerAddress.updated_at.desc(),
                  CustomerAddress.created_at.desc())
    )
    return list(result.scalars())


async def _carry_over(session: AsyncSession, customer: Customer) -> CustomerAddress:
    """Turn the single pre-existing address into the first saved one."""
    address = CustomerAddress(
        customer_id=customer.id,
        label=CARRIED_OVER_LABEL,
        address_line1=(customer.address_line1 or "")[:MAX_STREET],
        apartment_unit=customer.apartment_unit,
        postal_code=(customer.postal_code or "")[:MAX_ZIP],
        delivery_instructions=customer.delivery_instructions,
        latitude=customer.latitude,
        longitude=customer.longitude,
        is_default=True,
    )
    session.add(address)
    await session.flush()
    log.info("address_carried_over", customer_id=str(customer.id))
    return address


def _text(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _coordinate(value, bound: float) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if -bound <= number <= bound else None
