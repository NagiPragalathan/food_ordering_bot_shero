"""Tell the team on WhatsApp about every paid order.

The numbers are set on the admin Settings page (Order alerts) and stored as
ORDER_ALERT_NUMBERS, "number|name,..." - the same form as the Bot replies
whitelist, so services/allowlist parses it. Nothing set means no alerts.

Each number gets the `shero_new_order_alert` template. Until Meta approves it
the client sends the same words as an ordinary message (gallabox/fallback.py),
which WhatsApp only delivers to a number that wrote to the bot in the last
24 hours.

A failed alert never affects the order: the customer has paid, and the order
is on the dashboard whatever happens here.
"""

from __future__ import annotations

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Customer, Order
from app.integrations.gallabox import templates as tpl
from app.integrations.gallabox.sender import current_sender
from app.services import allowlist

log = get_logger(__name__)

# A template parameter is one line, and WhatsApp caps it; long carts are cut.
MAX_VALUE = 300


def entries() -> list[allowlist.Entry]:
    return allowlist.parse_entries(settings.order_alert_numbers)


def _line(value: object) -> str:
    """One clean line, never empty (Meta refuses a blank parameter)."""
    text = " ".join(str(value or "").split())
    return (text[:MAX_VALUE - 1] + "…") if len(text) > MAX_VALUE else (text or "-")


def _items(order: Order) -> str:
    lines = [f"{line.get('quantity', 1)} x {line.get('name', '?')}"
             for line in order.items or [] if isinstance(line, dict)]
    return ", ".join(lines)


def _address(order: Order) -> str:
    return ", ".join(p for p in (order.delivery_address, order.apartment_unit,
                                 order.postal_code) if p)


def values(order: Order, customer: Customer | None, kitchen_name: str) -> tuple[str, ...]:
    """The template's parameters, in ORDER_ALERT.params order."""
    who = ", ".join(p for p in ((customer.name if customer else None),
                                f"+{customer.whatsapp_number}" if customer else None) if p)
    return tuple(_line(v) for v in (
        order.order_number, who, f"{order.total:.2f}", order.slot_label,
        kitchen_name, _address(order), _items(order)))


async def notify_new_order(order: Order, customer: Customer | None, kitchen_name: str) -> int:
    """Send the alert to every configured number. Returns how many were sent."""
    numbers = entries()
    if not numbers:
        return 0
    params = values(order, customer, kitchen_name)
    sent = 0
    for entry in numbers:
        try:
            await current_sender().send_template(entry.number, tpl.ORDER_ALERT, *params)
            sent += 1
        except Exception as exc:  # noqa: BLE001 - one bad number must not stop the rest
            log.error("order_alert_failed", order_number=order.order_number,
                      number_tail=entry.number[-4:], error=str(exc))
    log.info("order_alerts_sent", order_number=order.order_number, sent=sent,
             of=len(numbers))
    return sent
