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
from app.schemas.inbound import parse_inbound
from app.services.conversation.engine import handle_event

log = get_logger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/gallabox", status_code=status.HTTP_200_OK)
async def gallabox_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
    authorization: str | None = Header(default=None),
    x_gallabox_token: str | None = Header(default=None),
) -> dict:
    """Receive one WhatsApp message and run it through the bot."""
    if not verify_gallabox_token(x_gallabox_token or authorization):
        log.warning("gallabox_webhook_rejected", reason="bad token")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid webhook token")

    try:
        body = await request.json()
    except ValueError:
        log.warning("gallabox_webhook_bad_json")
        return {"status": "ignored", "reason": "malformed json"}

    if not isinstance(body, dict):
        return {"status": "ignored", "reason": "unexpected payload"}

    event = parse_inbound(body)
    log.info("gallabox_webhook_received", kind=str(event.kind),
             message_id=event.message_id)

    handled = await handle_event(session, event)
    return {"status": "ok" if handled else "ignored"}


@router.get("/gallabox", include_in_schema=False)
async def gallabox_webhook_verify() -> dict:
    """Reachability check for configuring the webhook in Gallabox."""
    return {"status": "ready"}
