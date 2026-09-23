"""Drive the real bot from the terminal, sending nothing to WhatsApp.

The engine, handlers, menu and database are the real ones - only the outbound
integrations are swapped for recorders, so a conversation can be walked
through locally without messaging a real customer through the live Gallabox
account.

    python -m scripts.simulate_chat                  # interactive
    python -m scripts.simulate_chat hi Prem a@b.com  # scripted, one arg per turn

At the prompt:

    <text>            send a text message
    /tap <id>         tap a button or list row (prefix match, so `/tap cat:` works)
    /loc <lat> <lng>  drop a location pin
    /state            show the conversation step and cart
    /reset            forget this customer and start again from step 1
    /quit             leave

Uber and Stripe are stubbed with fixed values - real credentials for those are
not needed to exercise the conversation, and any message they produce is
marked [simulated].
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.db.models import Conversation, Customer, Outlet
from app.db.session import session_scope
from app.schemas.inbound import InboundEvent, InboundKind
from app.services.conversation.engine import handle_event

PHONE = "10000000001"


# --- recorders ---------------------------------------------------------------
@dataclass
class Sent:
    kind: str
    body: str = ""
    options: list[tuple[str, str]] = field(default_factory=list)


class ConsoleGallabox:
    """Collects what the bot would have sent, and the ids it offered to tap."""

    def __init__(self) -> None:
        self.sent: list[Sent] = []

    def _record(self, item: Sent) -> None:
        self.sent.append(item)

    async def send_text(self, to, body, name=None):
        self._record(Sent("text", body))

    async def send_buttons(self, to, body, buttons, header=None, footer=None):
        self._record(Sent("buttons", body, [(b.id, b.title) for b in buttons]))

    async def send_list(self, to, body, sections, button_text="Choose",
                        header=None, footer=None):
        rows = [(r.id, r.title) for s in sections for r in s.rows]
        self._record(Sent("list", body, rows))

    async def request_location(self, to, body):
        self._record(Sent("location_request", body))

    async def send_product_list(self, to, sections, header, body, footer=None):
        ids = [(p["product_retailer_id"], "") for s in sections
               for p in s["product_items"]]
        self._record(Sent("products", body, ids))

    async def send_template(self, to, spec, *values, button_value=None):
        rendered = "template " + spec.name + ": " + " | ".join(str(v) for v in values)
        self._record(Sent("template", rendered))

    async def handover_to_agent(self, to, note=None):
        self._record(Sent("handover", note or "handed to a human agent"))

    def drain(self) -> list[Sent]:
        out, self.sent = self.sent, []
        return out


def install_fakes() -> ConsoleGallabox:
    """Swap the outbound integrations for recorders, as the tests do."""
    from app.integrations.uber import direct as uber_direct_module
    from app.integrations.uber.direct import DeliveryQuote
    from app.services import payments
    from app.services.conversation import context as context_module

    fake = ConsoleGallabox()
    context_module.gallabox = fake
    payments.gallabox = fake

    async def quote(**kwargs):
        return DeliveryQuote(quote_id="dqt_sim", fee=Decimal("5.99"),
                             extra_fees=Decimal("1.20"), currency="USD")

    uber_direct_module.uber_direct.get_quote = quote

    async def create_session(**kwargs):
        return {
            "id": "cs_sim_123",
            "url": "https://checkout.stripe.com/c/pay/cs_sim_123  [simulated]",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=30),
        }

    payments.checkout.create_checkout_session = create_session
    return fake


# --- rendering ---------------------------------------------------------------
def show(messages: list[Sent]) -> None:
    if not messages:
        print("   (the bot said nothing)")
    for msg in messages:
        print()
        for line in (msg.body or "").splitlines() or [""]:
            print("   " + line)
        if msg.kind == "location_request":
            print("   [asks for a location pin - reply with /loc <lat> <lng>]")
        for option_id, title in msg.options:
            print("     [" + option_id + "]  " + title)
    print()


def parse(line: str, tappable: list[tuple[str, str]]) -> InboundEvent | None:
    """Turn one line of terminal input into an inbound WhatsApp event."""
    event = InboundEvent(whatsapp_number=PHONE, contact_name="Test Customer")
    event.message_id = "sim-" + str(datetime.now(timezone.utc).timestamp())

    if line.startswith("/tap "):
        wanted = line[5:].strip()
        match = next((i for i, _ in tappable if i == wanted), None)
        if match is None:
            match = next((i for i, _ in tappable if i.startswith(wanted)), None)
        if match is None:
            # Nothing offered in this run matches. A real customer can still
            # tap a button from further up the chat, and the engine is built
            # to recognise a stale id, so send it through verbatim rather than
            # refusing - that path is worth being able to test.
            if ":" not in wanted:
                print("   no option starting with " + repr(wanted) + ". Options: "
                      + str([i for i, _ in tappable]))
                return None
            match = wanted
        event.kind = InboundKind.REPLY
        event.reply_id = match
        event.reply_title = next((t for i, t in tappable if i == match), "")
        return event

    if line.startswith("/loc "):
        parts = line.split()
        if len(parts) != 3:
            print("   usage: /loc <lat> <lng>")
            return None
        event.kind = InboundKind.LOCATION
        event.latitude, event.longitude = float(parts[1]), float(parts[2])
        return event

    event.kind = InboundKind.TEXT
    event.text = line
    return event


async def print_state() -> None:
    async with session_scope() as session:
        # Conversations key on the customer, not the number, so join through.
        convo = await session.scalar(
            select(Conversation)
            .join(Customer, Customer.id == Conversation.customer_id)
            .where(Customer.whatsapp_number == PHONE)
        )
        if convo is None:
            print("   no conversation yet\n")
            return
        cart = (convo.context or {}).get("cart", [])
        print("   step: " + str(convo.step))
        print("   cart: " + str(len(cart)) + " line(s)")
        for line in cart:
            name = line.get("name") or line.get("retailer_id")
            print("     " + str(line.get("quantity")) + "x " + str(name))
        print()


async def reset() -> None:
    """Delete the simulated customer so the next message starts at step 1.

    Without this the flow resumes mid-conversation, and the first thing typed
    gets taken as the answer to whatever question was pending.
    """
    async with session_scope() as session:
        customer = await session.scalar(
            select(Customer).where(Customer.whatsapp_number == PHONE)
        )
        if customer is None:
            print("   nothing to reset\n")
            return
        await session.delete(customer)   # conversations cascade
    print("   reset - the next message starts a new conversation\n")


async def warn_if_unconfigured() -> None:
    """A missing kitchen stops the flow at the serviceability check."""
    async with session_scope() as session:
        kitchen = await session.scalar(select(Outlet).limit(1))
    if kitchen is None:
        print("! No kitchen configured, so the flow will stop at the delivery-area\n"
              "  check. Add one at /admin/settings (Kitchen & delivery area).\n")


def _force_utf8_output() -> None:
    """The bot's copy contains emoji; the Windows console defaults to cp1252.

    Without this, printing the very first reply raises UnicodeEncodeError and
    the simulator dies on a message the real WhatsApp client renders fine.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass  # a redirected stream that cannot be reconfigured


async def main(argv: list[str]) -> int:
    _force_utf8_output()
    fake = install_fakes()
    await warn_if_unconfigured()

    scripted = list(argv)
    tappable: list[tuple[str, str]] = []

    print("Simulating WhatsApp " + PHONE + ". /quit to leave, /state to inspect.\n")

    while True:
        if scripted:
            line = scripted.pop(0)
            print("> " + line)
        elif sys.stdin.isatty():
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
        else:
            return 0

        if not line:
            continue
        if line in {"/quit", "/exit"}:
            return 0
        if line == "/state":
            await print_state()
            continue
        if line == "/reset":
            await reset()
            tappable = []
            continue

        event = parse(line, tappable)
        if event is None:
            continue

        try:
            async with session_scope() as session:
                await handle_event(session, event)
        except Exception as exc:  # the engine handles flow errors; this is a bug
            print("   !! " + type(exc).__name__ + ": " + str(exc) + "\n")
            continue

        messages = fake.drain()
        show(messages)
        for msg in reversed(messages):
            if msg.options:
                tappable = msg.options
                break


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
