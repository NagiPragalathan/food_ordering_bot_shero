"""Delivery and read receipts for nudges (handoff section 11: "Delivery/read
status: provider status and timestamp, when available").

Gallabox posts a status event to the same webhook as inbound messages when
"Message.WA.Status.*" events are switched on in its Settings > Webhooks. The
send stored Gallabox's message id (nudge_sends.provider_message_id); a
receipt naming that id updates the send's delivery status. Receipts for
anything else - order updates, the bot's replies - are ignored.

Gallabox does not publish the payload's exact shape, so the id and status are
looked for where WhatsApp providers put them, and a receipt that cannot be
read is logged by its keys (never its content) so it can be matched up.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import NudgeSend

log = get_logger(__name__)

# Provider words -> ours, in the order a message moves through them.
STATUSES = {"sent": "sent", "delivered": "delivered", "read": "read", "seen": "read",
            "failed": "failed", "undelivered": "failed", "rejected": "failed", "error": "failed"}
RANK = {"sent": 1, "delivered": 2, "read": 3, "failed": 4}


def _places(body: dict) -> list[dict]:
    """The parts of a payload a status and an id may sit in."""
    places = [body]
    for key in ("data", "message", "whatsapp", "payload"):
        value = body.get(key)
        if isinstance(value, dict):
            places.append(value)
    statuses = body.get("statuses")
    if not isinstance(statuses, list) and isinstance(body.get("whatsapp"), dict):
        statuses = body["whatsapp"].get("statuses")
    if isinstance(statuses, list) and statuses and isinstance(statuses[0], dict):
        places.insert(0, statuses[0])
    return places


def read(body: dict) -> tuple[str | None, str | None]:
    """(message id, our status) from a receipt, as far as it can be read."""
    message_id = status = None
    for place in _places(body):
        if message_id is None:
            for name in ("messageId", "message_id", "id", "wamid", "_id"):
                if place.get(name):
                    message_id = str(place[name])
                    break
        if status is None:
            raw = str(place.get("status") or place.get("messageStatus") or "").strip().lower()
            status = STATUSES.get(raw)
    return message_id, status


def is_status_event(event_name: str, body: dict) -> bool:
    name = (event_name or "").lower()
    if "status" in name:
        return True
    return isinstance(body.get("statuses"), list)


async def record(session: AsyncSession, body: dict) -> bool:
    message_id, status = read(body)
    if not message_id or not status:
        log.info("delivery_status_unreadable", body_keys=sorted(body.keys()))
        return False
    send = (await session.execute(select(NudgeSend).where(
        NudgeSend.provider_message_id == message_id))).scalar_one_or_none()
    if send is None:
        return False             # not a nudge: an order update or a bot reply
    # Receipts can arrive out of order: "read" is not undone by a late "delivered".
    if RANK.get(status, 0) <= RANK.get(send.delivery_status or "", 0) and status != "failed":
        return True
    send.delivery_status = status
    send.delivery_at = datetime.now(timezone.utc)
    await session.flush()
    log.info("nudge_delivery_status", send_id=str(send.id), status=status)
    return True
