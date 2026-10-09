"""Inbound WhatsApp webhook (Gallabox).

Always answers 200 once the caller is authenticated. Gallabox retries on any
non-2xx, and a retry of a message we already partly processed is worse than a
logged failure - the engine catches its own errors and tells the customer, so
there is nothing useful for a retry to fix.

Genuine infrastructure failures (database down) do return 500, because those
*are* worth retrying.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.security import verify_gallabox_token
from app.db.session import get_session
from app.integrations.gallabox import channel
from app.schemas.inbound import parse_inbound
from app.services import allowlist, reply_triggers
from app.services.conversation.engine import handle_event

log = get_logger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/gallabox", status_code=status.HTTP_200_OK)
async def gallabox_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
    authorization: str | None = Header(default=None),
    x_gallabox_token: str | None = Header(default=None),
    x_webhook_secret: str | None = Header(default=None),
    token: str | None = None,
) -> dict:
    """Receive one WhatsApp message and run it through the bot.

    The token may arrive as `X-Gallabox-Token`, `Authorization`,
    `X-Webhook-Secret`, or a `?token=` query parameter. Several webhook
    consoles let you set only a URL, so the query form is there as a fallback
    - it is the same shared secret either way, though it does end up in access
    logs, so a header is preferable where the console allows one.
    """
    supplied = x_gallabox_token or authorization or x_webhook_secret or token
    if not verify_gallabox_token(supplied):
        # Name the headers that arrived (never their values) so a misconfigured
        # sender can be diagnosed without a guessing game.
        log.warning("gallabox_webhook_rejected", reason="bad token",
                    headers_seen=sorted(request.headers.keys()),
                    had_query_token=bool(token))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid webhook token")

    try:
        body = await request.json()
    except ValueError:
        log.warning("gallabox_webhook_bad_json")
        return {"status": "ignored", "reason": "malformed json"}

    if not isinstance(body, dict):
        return {"status": "ignored", "reason": "unexpected payload"}

    event = parse_inbound(body)

    # Another team's WhatsApp number in the same Gallabox account: not ours to
    # answer, record or send to Zoho. Checked first of all.
    if not channel.is_ours(event):
        log.info("other_channel_ignored", message_id=event.message_id,
                 channel_id=event.channel_id, channel_number_tail=event.channel_number[-4:])
        return {"status": "ignored", "reason": "another channel"}

    # Test mode: answer only the listed numbers. Checked before anything else
    # happens, so another customer gets no record, no reply and no Zoho lead -
    # their message simply carries on to your team in Gallabox.
    if event.whatsapp_number and not allowlist.permits(event.whatsapp_number):
        # Last four digits only: enough to tell the tester from a customer
        # without writing strangers' full numbers into the logs.
        log.info("sender_not_allowlisted", message_id=event.message_id,
                 number_tail=event.whatsapp_number[-4:])
        return {"status": "ignored", "reason": "sender not whitelisted"}

    # Keyword mode: a chat only starts the bot on a trigger keyword. Like the
    # whitelist, a message that does not is left for your team untouched.
    decision = await reply_triggers.decide(session, event)
    if not decision.answer:
        log.info("message_without_trigger", message_id=event.message_id,
                 number_tail=event.whatsapp_number[-4:])
        return {"status": "ignored", "reason": "no trigger keyword"}

    log.info("gallabox_webhook_received", kind=str(event.kind),
             message_id=event.message_id, reply_reason=decision.reason)

    if not event.is_actionable:
        # An envelope we could not read looks identical to a delivered message
        # from Gallabox's side: it gets its 200 and never retries, while the
        # customer waits for a reply that is never coming. Log the shape (keys
        # only - the body carries the customer's own words and number) so the
        # gap is diagnosable from the log rather than by guesswork.
        log.warning("gallabox_webhook_unreadable",
                    reason="no number" if not event.whatsapp_number else "unknown type",
                    body_keys=sorted(body.keys()),
                    whatsapp_keys=sorted((body.get("whatsapp") or {}).keys())
                    if isinstance(body.get("whatsapp"), dict) else [],
                    event_name=str(body.get("event") or body.get("type") or ""))

    handled = await handle_event(session, event, fresh_start=decision.fresh_start,
                                 reply=decision.reply)
    return {"status": "ok" if handled else "ignored"}


@router.get("/gallabox", include_in_schema=False)
async def gallabox_webhook_verify() -> dict:
    """Reachability check for configuring the webhook in Gallabox."""
    return {"status": "ready"}
