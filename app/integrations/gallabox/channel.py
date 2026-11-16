"""Which Gallabox channel (WhatsApp business number) a message came in on.

Gallabox's webhook belongs to the account, not the channel: it delivers the
messages of *every* WhatsApp number in the account - other teams' numbers
included - to the same URL. The bot must only ever answer its own channel
(GALLABOX_CHANNEL_ID, the number in WHATSAPP_BUSINESS_NUMBER), or it greets
other teams' customers, asks them for their name and writes them into Zoho.

Every Gallabox message carries `channelId` and `channelNumber` at the top of
the body. A message without either cannot be placed, so it is refused too:
answering a stranger is worse than missing a message.
"""

from __future__ import annotations

from app.core import tenants
from app.core.config import is_unset, settings, settings_for
from app.schemas.inbound import InboundEvent


def _digits(raw: str) -> str:
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def our_channel_id() -> str:
    raw = settings.gallabox_channel_id
    return "" if is_unset(raw) else raw.strip()


def is_ours(event: InboundEvent) -> bool:
    """Did this message arrive on the bot's own channel?

    The channel id decides when both sides have one; the number is the
    fallback. With neither configured (a fresh local setup) there is nothing
    to compare against, so everything counts as ours.
    """
    ours_id = our_channel_id()
    ours_number = _digits(settings.whatsapp_business_number)
    if not ours_id and not ours_number:
        return True
    return _matches(event, ours_id, ours_number)


def _matches(event: InboundEvent, ours_id: str, ours_number: str) -> bool:
    if event.channel_id and ours_id:
        return event.channel_id == ours_id
    if event.channel_number and ours_number:
        return _digits(event.channel_number) == ours_number
    return False


def tenant_for(event: InboundEvent) -> tenants.Tenant | None:
    """The business whose WhatsApp number this message came in on, or None.

    Shero US and DST India are two numbers in one Gallabox account, and
    Gallabox sends both numbers' messages to the same webhook. Another
    business claims a message only by its own configured channel, never by
    having none set; the business being served then answers as before
    (`is_ours`, a fresh setup included). None: another team's number.
    """
    here = tenants.current()
    for tenant in tenants.enabled():
        if tenant == here:
            continue
        own = settings_for(tenant)
        own_id = "" if is_unset(own.gallabox_channel_id) else own.gallabox_channel_id.strip()
        own_number = _digits(own.whatsapp_business_number)
        if (own_id or own_number) and _matches(event, own_id, own_number):
            return tenant
    return here if is_ours(event) else None
