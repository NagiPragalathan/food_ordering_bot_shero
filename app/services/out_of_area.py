"""An order address that no kitchen delivers to.

The ordering page refuses the address with a message naming it, and the
customer is also told on WhatsApp (the shero_out_of_area template): we are
not in their area yet, and hope to be soon.

Not spam: the WhatsApp message goes at most once every NOTICE_EVERY per
customer. Someone trying address after address sees the refusal on the page
each time, but gets one WhatsApp message, not one per try.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.logging import get_logger
from app.db.models import Customer
from app.db.models.address import CustomerAddress
from app.integrations.gallabox import templates as tpl
from app.integrations.gallabox.sender import current_sender
from app.services.kitchen import ServiceCheck, miles

log = get_logger(__name__)

NOTICE_EVERY = timedelta(hours=24)


def address_line(address: CustomerAddress) -> str:
    """Street, unit and ZIP on one line: the address as the customer typed it."""
    parts = [address.address_line1, address.apartment_unit, address.postal_code]
    return ", ".join(p.strip() for p in parts if p and p.strip()) or "that address"


def page_message(check: ServiceCheck, where: str) -> str:
    """What the ordering page says, in miles."""
    text = f"Sorry, we do not deliver to {where} yet."
    away = miles(check.distance_km)
    if away is not None:
        text += f" Our nearest kitchen is about {away:g} miles away, outside its delivery area."
    return text + " Please try a different address."


def details(check: ServiceCheck, where: str, *, notified: bool) -> dict:
    """What the ordering page needs to draw its "not here yet" panel."""
    return {
        "address": where,
        "miles": miles(check.distance_km),
        "range_miles": miles(check.range_km),
        "notified": notified,
    }


async def notify(customer: Customer, where: str, *, now: datetime | None = None) -> bool:
    """Tell the customer on WhatsApp, unless they were told within NOTICE_EVERY.

    Returns True if a message went. A failed send is logged and never breaks
    the page; the customer has the page's own message either way.
    """
    now = now or datetime.now(timezone.utc)
    last = customer.out_of_area_notified_at
    if last is not None:
        # SQLite hands back naive datetimes; they were stored as UTC.
        last = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
        if now - last < NOTICE_EVERY:
            log.info("out_of_area_notice_skipped", customer_id=str(customer.id),
                     last_sent=last.isoformat())
            return False

    try:
        await current_sender().send_template(customer.whatsapp_number, tpl.OUT_OF_AREA,
                                             customer.greeting_name, where)
    except Exception as exc:  # noqa: BLE001 - the page already told them
        log.error("out_of_area_notice_failed", customer_id=str(customer.id), error=str(exc))
        return False

    customer.out_of_area_notified_at = now
    log.info("out_of_area_notice_sent", customer_id=str(customer.id))
    return True
