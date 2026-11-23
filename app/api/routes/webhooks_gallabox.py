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

from app.core import tenants
from app.core.logging import get_logger
from app.core.security import verify_gallabox_token
from app.db.session import get_session, session_scope
from app.integrations.gallabox import channel
from app.schemas.inbound import parse_inbound
from app.services import allowlist, reply_triggers, welcome_menu
from app.services.conversation.engine import handle_event
from app.services.nudges import delivery
from app.sft import router as sft

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
    event_name = request.headers.get("x-event-name") or str(body.get("event") or "")

    # Shero Food Training (app/sft/) first: its own number, or a chat that
    # asks for it on Shero US's. Off, or not SFT's: it changes nothing here.
    if tenants.current().is_default and not delivery.is_status_event(event_name, body):
        taken = await sft.route(session, event)
        if taken is not None:
            return taken

    # Which business's WhatsApp number it came in on (Shero US, DST India -
    # one Gallabox account, one webhook). Another team's number in the same
    # account is not ours to answer, record or send to a CRM. Checked first.
    tenant = channel.tenant_for(event)
    if tenant is None and delivery.is_status_event(event_name, body):
        # A receipt only ever updates a message this business sent (matched
        # by its message id), so one that names no channel is checked here
        # as it always was.
        tenant = tenants.current()
    if tenant is None:
        log.info("other_channel_ignored", message_id=event.message_id,
                 channel_id=event.channel_id, channel_number_tail=event.channel_number[-4:])
        return {"status": "ignored", "reason": "another channel"}
    if tenant != tenants.current():
        # The other business: its own settings and its own database.
        with tenants.use(tenant):
            async with session_scope() as own_session:
                return await _handle(body, event, event_name, own_session)
    return await _handle(body, event, event_name, session)


async def _handle(body: dict, event, event_name: str, session: AsyncSession) -> dict:
    """One message (or receipt) on one business's own number."""
    # A delivery or read receipt for a message we sent (Gallabox's
    # Message.WA.Status.* events), not a message from the customer.
    if delivery.is_status_event(event_name, body):
        updated = await delivery.record(session, body)
        return {"status": "ok" if updated else "ignored", "reason": "delivery status"}

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
    number = event.whatsapp_number
    # The welcome menu: a new chat gets it; a button for a Gallabox flow is
    # left to that flow (Gallabox starts it on the button's words).
    if decision.action == welcome_menu.SEND:
        sent = await welcome_menu.send(session, number)
        return {"status": "ok" if sent else "ignored", "reason": "welcome menu"}
    if decision.action == welcome_menu.LEFT_TO_GALLABOX:
        await welcome_menu.left_to_gallabox(session, number, decision.label)
        return {"status": "ignored", "reason": "Gallabox flow"}
    if not decision.answer:
        log.info("message_without_trigger", message_id=event.message_id,
                 number_tail=number[-4:], reason=decision.reason)
        return {"status": "ignored", "reason": "no trigger keyword"}
    if decision.fresh_start:
        await welcome_menu.back_to_bot(session, number)

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
