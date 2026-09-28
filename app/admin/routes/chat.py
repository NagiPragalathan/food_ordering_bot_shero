"""Chat tester: talk to the bot from the browser.

Drives the real engine, handlers, menu and database against a reserved test
phone number, with outbound messages collected instead of delivered. Nothing
reaches the live Gallabox account, so this is safe to use on a deployment that
is serving real customers.

The test conversation is a real row in the database like any other, which is
the point - it exercises the same code path a WhatsApp customer takes. `Reset`
deletes that customer so the next message starts at step 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import render, require_admin
from app.core.logging import get_logger
from app.db.models import (AdminUser, Conversation, ConversationStep,
                            Customer, Outlet)
from app.db.session import get_session
from app.integrations.gallabox.sender import use_sender
from app.schemas.inbound import InboundEvent, InboundKind
from app.services.conversation.engine import handle_event

log = get_logger(__name__)
router = APIRouter(prefix="/chat", tags=["admin"])

# Reserved for the tester. Not a routable number, so a stray real send to it
# would fail rather than message a stranger.
TEST_NUMBER = "10000000001"


@dataclass
class Recorder:
    """Stands in for the Gallabox client and keeps what the bot said."""

    sent: list[dict] = field(default_factory=list)

    async def send_text(self, to, body, name=None):
        self.sent.append({"kind": "text", "body": body, "options": []})

    async def send_buttons(self, to, body, buttons, header=None, footer=None):
        self.sent.append({
            "kind": "buttons", "body": body,
            "options": [{"id": b.id, "title": b.title} for b in buttons],
        })

    async def send_list(self, to, body, sections, button_text="Choose",
                        header=None, footer=None):
        self.sent.append({
            "kind": "list", "body": body,
            "options": [{"id": r.id, "title": r.title}
                        for s in sections for r in s.rows],
        })

    async def send_cta_url(self, to, body, *, url, display_text,
                           header=None, footer=None):
        # The tester shows the destination, which a real customer never
        # sees - it is the one thing you want to check here.
        self.sent.append({
            "kind": "cta_url",
            "body": f"{body}\n\n[{display_text}] -> {url}",
            "options": [],
        })

    async def request_location(self, to, body):
        self.sent.append({"kind": "location_request", "body": body, "options": []})

    async def send_product_list(self, to, sections, header, body, footer=None):
        self.sent.append({
            "kind": "list", "body": body,
            "options": [{"id": p["product_retailer_id"], "title": p.get("title", "")}
                        for s in sections for p in s["product_items"]],
        })

    async def send_template(self, to, spec, *values, button_value=None):
        body = f"[template: {spec.name}]\n" + "\n".join(str(v) for v in values)
        self.sent.append({"kind": "template", "body": body, "options": []})

    async def handover_to_agent(self, to, note=None):
        self.sent.append({
            "kind": "handover",
            "body": note or "Handed over to a human agent.",
            "options": [],
        })


@router.get("", name="admin_chat")
async def chat_page(request: Request, session: AsyncSession = Depends(get_session),
                    current_user: AdminUser = Depends(require_admin)):
    """The tester page. Warns up front about the setup gaps that stop it."""
    kitchen = await session.scalar(select(Outlet).limit(1))
    step = await _step(session)
    return render(request, "admin/chat.html", {
        "current_user": current_user,
        "test_number": TEST_NUMBER,
        "has_kitchen": kitchen is not None,
        "has_hours": bool(kitchen and kitchen.operating_hours),
        # An unfinished conversation is why the welcome message would not
        # appear: the bot answers the question it last asked instead.
        "resuming": step != "start",
        "step": step,
    })


@router.post("/send", name="admin_chat_send")
async def chat_send(request: Request, session: AsyncSession = Depends(get_session),
                    current_user: AdminUser = Depends(require_admin)) -> dict:
    """Run one message through the bot and return what it replied."""
    body = await request.json()
    kind = str(body.get("kind") or "text")

    event = InboundEvent(whatsapp_number=TEST_NUMBER, contact_name="Test Customer")
    event.message_id = f"admin-chat-{datetime.now(timezone.utc).timestamp()}"

    if kind == "reply":
        event.kind = InboundKind.REPLY
        event.reply_id = str(body.get("id") or "")
        event.reply_title = str(body.get("title") or "")
    elif kind == "location":
        event.kind = InboundKind.LOCATION
        try:
            event.latitude = float(body.get("latitude"))
            event.longitude = float(body.get("longitude"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "Latitude and longitude must be numbers."}
    else:
        event.kind = InboundKind.TEXT
        event.text = str(body.get("text") or "").strip()
        if not event.text:
            return {"ok": False, "error": "Type something first."}

    recorder = Recorder()
    try:
        with use_sender(recorder):
            await handle_event(session, event)
    except Exception as exc:
        # The engine handles expected flow failures itself and replies to the
        # customer, so reaching here means a genuine bug. Surface it in the
        # page rather than leaving the tester silently stuck.
        log.exception("admin_chat_failed", error=str(exc))
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "messages": recorder.sent}

    step = await _step(session)
    return {
        "ok": True,
        "messages": recorder.sent,
        "step": step,
        # A silent bot is sometimes correct, so say which case this is rather
        # than leaving the tester looking broken.
        "note": None if recorder.sent else _why_silent(step),
    }


def _why_silent(step: str) -> str:
    """Explain a turn that produced no outbound message."""
    if step == str(ConversationStep.HANDED_OVER):
        return ("The bot is staying quiet because this conversation was handed "
                "to a human agent. That is the designed behaviour - press "
                "Reset to test again.")
    return (f"The bot sent nothing at step '{step}'. That is usually a bug - "
            f"check the server log.")


@router.post("/reset", name="admin_chat_reset")
async def chat_reset(session: AsyncSession = Depends(get_session),
                     current_user: AdminUser = Depends(require_admin)) -> dict:
    """Forget the test customer so the next message starts at step 1."""
    customer = await session.scalar(
        select(Customer).where(Customer.whatsapp_number == TEST_NUMBER)
    )
    if customer is not None:
        await session.delete(customer)   # conversation cascades
        log.info("admin_chat_reset", by=current_user.email)
    return {"ok": True}


async def _step(session: AsyncSession) -> str:
    """The conversation step, shown in the page header while testing."""
    convo = await session.scalar(
        select(Conversation)
        .join(Customer, Customer.id == Conversation.customer_id)
        .where(Customer.whatsapp_number == TEST_NUMBER)
    )
    return str(convo.step) if convo else "start"
