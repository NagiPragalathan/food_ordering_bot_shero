"""End-to-end walk through the ordering flow (spec steps 1-15).

Drives the real engine, real handlers, real menu and a real database. Only the
outbound integrations are faked, and every WhatsApp message is captured so the
conversation can be asserted step by step.

Zoho is deliberately *not* faked: with no credentials configured it raises
ConfigurationError, which proves the "CRM sync never breaks the conversation"
rule holds for the whole happy path.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.integrations.gallabox import templates as tpl

from app.db.models import ConversationStep, LeadStage, PaymentStatus
from app.schemas.inbound import CartLine, InboundEvent, InboundKind
from app.services.conversation.engine import handle_event

# Retailer ids built by the `menu` fixture in conftest.
DRUMSTICK = "chettinad-sambar-drumstick-sambar"
BEANS = "chettinad-sambar-beans-sambar"
RASAM = "chettinad-rasam-tomato-rasam"


# --- fakes -------------------------------------------------------------------
@dataclass
class SentMessage:
    kind: str
    to: str
    body: str = ""
    payload: dict = field(default_factory=dict)


class FakeGallabox:
    """Records what the bot would have sent."""

    def __init__(self) -> None:
        self.sent: list[SentMessage] = []

    async def send_text(self, to, body, name=None):
        self.sent.append(SentMessage("text", to, body))

    async def send_buttons(self, to, body, buttons, header=None, footer=None):
        self.sent.append(SentMessage(
            "buttons", to, body, {"buttons": [(b.id, b.title) for b in buttons]}))

    async def send_list(self, to, body, sections, button_text="Choose",
                        header=None, footer=None):
        rows = [(r.id, r.title) for s in sections for r in s.rows]
        self.sent.append(SentMessage("list", to, body, {"rows": rows}))

    async def send_cta_url(self, to, body, *, url, display_text,
                           header=None, footer=None):
        self.sent.append(SentMessage("cta_url", to, body,
                                     {"url": url, "label": display_text}))

    async def request_location(self, to, body):
        self.sent.append(SentMessage("location_request", to, body))

    async def send_product_list(self, to, sections, header, body, footer=None):
        ids = [p["product_retailer_id"] for s in sections for p in s["product_items"]]
        self.sent.append(SentMessage("products", to, body, {"products": ids}))

    async def send_template(self, to, spec, *values, button_value=None):
        self.sent.append(SentMessage("template", to, spec.name, {
            "values": [str(v) for v in values], "button_value": button_value}))

    async def handover_to_agent(self, to, note=None):
        self.sent.append(SentMessage("handover", to, note or ""))

    # -- assertion helpers --
    def last(self) -> SentMessage:
        assert self.sent, "no message was sent"
        return self.sent[-1]

    def rows(self) -> list[tuple[str, str]]:
        return self.last().payload.get("rows", [])

    def row_id(self, prefix: str) -> str:
        """First row id starting with `prefix`, for tapping a list option."""
        for row_id, _ in self.rows():
            if row_id.startswith(prefix):
                return row_id
        raise AssertionError(f"no row starting with {prefix!r} in {self.rows()}")

    def kinds(self) -> list[str]:
        return [m.kind for m in self.sent]

    def clear(self) -> None:
        self.sent.clear()


@pytest.fixture
def bot(monkeypatch):
    """Patch every outbound integration and return the message recorder."""
    from app.core.config import settings
    from app.integrations.gallabox.sender import use_sender
    from app.integrations.uber import direct as uber_direct_module
    from app.integrations.uber.direct import DeliveryQuote
    from app.services import payments

    fake = FakeGallabox()

    # These tests walk the in-chat browsing flow. The shipped default is
    # ORDER_MODE=web, which sends a storefront link instead, so pin the mode
    # rather than letting a config default decide what is under test.
    monkeypatch.setattr(settings, "order_mode", "chat")

    async def quote(**kwargs):
        return DeliveryQuote(quote_id="dqt_test", fee=Decimal("5.99"),
                             extra_fees=Decimal("1.20"), currency="USD")

    monkeypatch.setattr(uber_direct_module.uber_direct, "get_quote", quote)

    async def create_session(**kwargs):
        return {
            "id": "cs_test_123",
            "url": "https://checkout.stripe.com/c/pay/cs_test_123",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=30),
        }

    monkeypatch.setattr(payments.checkout, "create_checkout_session", create_session)

    # One override covers the conversation handlers *and* the payment sends,
    # because both now resolve through `current_sender()`.
    with use_sender(fake):
        yield fake


# --- event helpers -----------------------------------------------------------
_seq = iter(range(1, 100_000))
PHONE = "17325550142"
# A pin close to the Edison kitchen fixture (40.5187, -74.4121).
NEARBY = (40.5300, -74.4000)
# Philadelphia: far outside any radius in the fixtures.
FAR = (39.9526, -75.1652)


def _event(**kwargs) -> InboundEvent:
    return InboundEvent(whatsapp_number=PHONE,
                        message_id=f"m{next(_seq)}", **kwargs)


def text(body: str) -> InboundEvent:
    return _event(kind=InboundKind.TEXT, text=body)


def reply(reply_id: str) -> InboundEvent:
    return _event(kind=InboundKind.REPLY, reply_id=reply_id)


def pin(lat: float, lng: float) -> InboundEvent:
    return _event(kind=InboundKind.LOCATION, latitude=lat, longitude=lng)


def native_cart(*lines: tuple[str, int]) -> InboundEvent:
    return _event(kind=InboundKind.CART,
                  cart_lines=[CartLine(r, q) for r, q in lines])


async def _state(session):
    from app.services.customers import get_customer, get_or_create_conversation

    customer = await get_customer(session, PHONE)
    conversation = await get_or_create_conversation(session, customer)
    return customer, conversation


async def _sign_up(session, bot):
    """Steps 1-5, which every test needs before it gets interesting."""
    await handle_event(session, text("hi"))
    await handle_event(session, text("Asha Menon"))
    await handle_event(session, text("asha@example.com"))


async def _add_drumstick(session, bot, quantity: str = "2"):
    """Steps 6-8: browse to a dish and put it in the cart."""
    await handle_event(session, reply("menu:order"))
    await handle_event(session, reply("cuisine:chettinad"))
    await handle_event(session, reply(bot.row_id("cat:")))
    await handle_event(session, reply(f"item:{DRUMSTICK}"))
    await handle_event(session, text(quantity))


# --- the walk ----------------------------------------------------------------
async def test_full_order_journey(session, outlet, menu, bot):
    # Steps 1-3: first contact from a Click-to-WhatsApp ad.
    first = _event(kind=InboundKind.TEXT, text="hi", ad_id="ad_99",
                   campaign_id="camp_7")
    assert await handle_event(session, first) is True

    customer, conversation = await _state(session)
    assert customer.ad_id == "ad_99"
    assert customer.lead_source == "Meta Ad"
    assert conversation.step == ConversationStep.AWAIT_NAME
    assert "Welcome to Shero" in bot.last().body

    # Step 3: name, with one invalid attempt first.
    await handle_event(session, text("x"))
    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.AWAIT_NAME, "should re-ask"

    await handle_event(session, text("Asha Menon"))
    customer, conversation = await _state(session)
    assert customer.name == "Asha Menon"
    assert conversation.step == ConversationStep.AWAIT_EMAIL

    # Step 4: email, with one invalid attempt.
    await handle_event(session, text("not-an-email"))
    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.AWAIT_EMAIL, "should re-ask"

    await handle_event(session, text("asha@example.com"))
    customer, conversation = await _state(session)
    assert customer.email == "asha@example.com"
    assert customer.lead_stage == LeadStage.DETAILS_CAPTURED
    assert conversation.step == ConversationStep.MAIN_MENU

    # Step 5: main menu.
    # One button: ordering is what people came for. A human is still
    # reachable by typing "agent" - covered just below, and load-bearing
    # now that the Talk to Us button is gone from this step.
    assert [b[1] for b in bot.last().payload["buttons"]] == ["Order Now"]

    # Step 6: cuisines come from the imported menu, plus Check Availability.
    await handle_event(session, reply("menu:order"))
    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.CUISINE_MENU
    titles = [t for _, t in bot.rows()]
    assert "Chettinad" in titles and "Kerala" in titles
    assert "Check Availability" in titles

    # Step 7a: categories for the chosen cuisine.
    await handle_event(session, reply("cuisine:chettinad"))
    customer, conversation = await _state(session)
    assert customer.cuisine_preference == "chettinad"
    assert customer.lead_stage == LeadStage.CUISINE_SELECTED
    assert conversation.step == ConversationStep.BROWSING_CATEGORIES
    assert {t for _, t in bot.rows()} >= {"Sambar", "Rasam"}

    # Step 7b: dishes in that category, priced from the menu.
    await handle_event(session, reply("cat:sambar"))
    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.BROWSING_ITEMS
    assert {t for _, t in bot.rows()} >= {"Drumstick Sambar", "Beans Sambar"}

    # Step 8a: quantity.
    await handle_event(session, reply(f"item:{DRUMSTICK}"))
    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.AWAIT_QUANTITY
    assert "$12.50" in bot.last().body

    await handle_event(session, text("not a number"))
    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.AWAIT_QUANTITY, "should re-ask"

    await handle_event(session, text("2"))
    customer, conversation = await _state(session)
    assert customer.lead_stage == LeadStage.CART_CREATED
    assert conversation.step == ConversationStep.CART_REVIEW

    # Step 8b: cart review offers Checkout / Add more / Clear.
    review = bot.last()
    assert [b[1] for b in review.payload["buttons"]] == [
        "Checkout", "Add more", "Clear cart"]
    assert "2 x Drumstick Sambar - $25.00" in review.body

    # Step 9: checkout asks for a location.
    await handle_event(session, reply("cart:checkout"))
    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.AWAIT_LOCATION
    assert bot.last().kind == "location_request"

    # Steps 9-10: serviceable, so it goes straight to delivery details.
    # There is one kitchen now - no outlet to choose.
    await handle_event(session, pin(*NEARBY))
    customer, conversation = await _state(session)
    assert customer.preferred_outlet_id == outlet.id
    assert customer.lead_stage == LeadStage.OUTLET_SELECTED
    assert conversation.step == ConversationStep.AWAIT_ADDRESS

    # Step 12: the four delivery questions.
    await handle_event(session, text("12 Maple Street, Edison"))
    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.AWAIT_APARTMENT

    await handle_event(session, text("Apt 4B"))
    await handle_event(session, text("skip"))
    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.AWAIT_CONTACT_NUMBER

    await handle_event(session, text("same"))
    customer, conversation = await _state(session)
    assert customer.address_line1 == "12 Maple Street, Edison"
    assert customer.apartment_unit == "Apt 4B"
    assert customer.delivery_instructions is None
    assert customer.contact_number == PHONE

    # Step 13: slots.
    assert conversation.step == ConversationStep.AWAIT_SLOT_CHOICE
    slot_id = bot.row_id("slot:")

    # Step 14: summary with Confirm / Edit / Cancel.
    await handle_event(session, reply(slot_id))
    customer, conversation = await _state(session)
    assert customer.lead_stage == LeadStage.SLOT_SELECTED
    assert conversation.step == ConversationStep.AWAIT_SUMMARY_CONFIRM

    summary = bot.last()
    assert [b[1] for b in summary.payload["buttons"]] == ["Confirm", "Edit", "Cancel"]
    # 2 x 12.50 = 25.00 dishes + 5.99 delivery + 1.20 fees = 32.19
    assert "*Total: $32.19*" in summary.body
    assert "2 x Drumstick Sambar - $25.00" in summary.body

    # Step 15: confirm -> Stripe link on the payment_link template.
    await handle_event(session, reply("summary:confirm"))
    customer, conversation = await _state(session)
    assert conversation.step == ConversationStep.AWAIT_PAYMENT
    assert customer.lead_stage == LeadStage.PAYMENT_LINK_SENT

    template_msg = bot.last()
    assert template_msg.kind == "template"
    assert template_msg.body == tpl.PAYMENT_LINK.name
    assert template_msg.payload["values"][0] == "Asha Menon"
    assert template_msg.payload["values"][2] == "32.19"
    # The Pay Now button carries the order number, not the long Stripe URL.
    assert template_msg.payload["button_value"].startswith("SHO-")

    # The order is stored, priced and awaiting payment with its slot held.
    from app.services.orders import get_latest_for_customer

    order = await get_latest_for_customer(session, customer.id)
    assert order.total == Decimal("32.19")
    assert order.payment_status == PaymentStatus.LINK_SENT
    assert order.stripe_session_id == "cs_test_123"
    assert order.slot_id is not None

    from sqlalchemy import select

    from app.db.models import SlotHold, SlotHoldStatus

    holds = await session.execute(
        select(SlotHold).where(SlotHold.order_id == order.id))
    held = list(holds.scalars())
    assert len(held) == 1 and held[0].status == SlotHoldStatus.HELD


async def test_duplicate_webhook_delivery_is_ignored(session, outlet, menu, bot):
    event = text("hi")
    assert await handle_event(session, event) is True
    assert await handle_event(session, event) is False


async def test_talk_to_us_hands_over_and_stops_the_bot(session, outlet, menu, bot):
    await _sign_up(session, bot)
    bot.clear()

    await handle_event(session, reply("menu:talk"))
    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.HANDED_OVER
    assert "handover" in bot.kinds()

    # A further message must not wake the bot up over the agent.
    bot.clear()
    await handle_event(session, text("are you there?"))
    assert bot.sent == []


async def test_typing_agent_reaches_a_human_without_a_button(
        session, outlet, menu, bot):
    """The main menu offers only Order Now, so the typed word is the way out."""
    await _sign_up(session, bot)
    bot.clear()

    await handle_event(session, text("agent"))

    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.HANDED_OVER
    assert "handover" in bot.kinds()


async def test_a_stale_talk_to_us_button_is_still_honoured(
        session, outlet, menu, bot):
    """Somebody may tap a Talk to Us button sent before this step changed."""
    await _sign_up(session, bot)
    bot.clear()

    await handle_event(session, reply("menu:talk"))

    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.HANDED_OVER


async def test_out_of_range_location_ends_politely(session, far_outlet, menu, bot):
    """Only the far kitchen exists, so a nearby pin is out of its range."""
    await _sign_up(session, bot)
    await _add_drumstick(session, bot)
    await handle_event(session, reply("cart:checkout"))
    bot.clear()

    await handle_event(session, pin(*NEARBY))
    customer, conversation = await _state(session)
    assert customer.lead_stage == LeadStage.NOT_SERVICEABLE
    assert conversation.step == ConversationStep.COMPLETED
    assert "do not deliver to your area" in bot.last().body


async def test_zip_list_decides_serviceability(session, outlet, menu, bot):
    """With service_area_mode='zips', the ZIP list is what counts."""
    outlet.service_area_mode = "zips"
    outlet.service_zips = ["21075"]
    await session.flush()

    await _sign_up(session, bot)
    await _add_drumstick(session, bot)
    await handle_event(session, reply("cart:checkout"))
    bot.clear()

    # A pin 3 km away, but its ZIP is not in the list.
    await handle_event(session, pin(*NEARBY))
    customer, _ = await _state(session)
    assert customer.lead_stage == LeadStage.NOT_SERVICEABLE


async def test_check_availability_saves_location_and_skips_step_nine(
    session, outlet, menu, bot
):
    """Spec step 6a: a location captured early means step 9 is skipped."""
    await _sign_up(session, bot)
    await handle_event(session, reply("menu:order"))

    await handle_event(session, reply("cuisine:check_availability"))
    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.AWAIT_AVAILABILITY_LOCATION

    await handle_event(session, pin(*NEARBY))
    customer, conversation = await _state(session)
    assert customer.latitude == NEARBY[0]
    # Returns to the cuisine list, as the spec describes.
    assert conversation.step == ConversationStep.CUISINE_MENU

    # From here the location must never be asked for again.
    bot.clear()
    await _add_drumstick(session, bot)
    await handle_event(session, reply("cart:checkout"))

    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.AWAIT_ADDRESS
    assert "location_request" not in bot.kinds(), "step 9 should have been skipped"


async def test_adding_the_same_dish_twice_merges_the_line(session, outlet, menu, bot):
    await _sign_up(session, bot)
    await _add_drumstick(session, bot, quantity="2")
    await handle_event(session, reply("cart:more"))
    await handle_event(session, reply("cat:sambar"))
    await handle_event(session, reply(f"item:{DRUMSTICK}"))
    await handle_event(session, text("3"))

    _, conversation = await _state(session)
    cart = conversation.context["cart"]
    assert len(cart) == 1, "the same dish should merge, not duplicate"
    assert cart[0]["quantity"] == 5


async def test_clearing_the_cart_returns_to_browsing(session, outlet, menu, bot):
    await _sign_up(session, bot)
    await _add_drumstick(session, bot)

    await handle_event(session, reply("cart:clear"))
    _, conversation = await _state(session)
    assert not conversation.context.get("cart")
    assert conversation.step == ConversationStep.BROWSING_CATEGORIES


async def test_typed_dish_name_searches_the_menu(session, outlet, menu, bot):
    await _sign_up(session, bot)
    await handle_event(session, reply("menu:order"))
    await handle_event(session, reply("cuisine:chettinad"))
    bot.clear()

    await handle_event(session, text("drumstick"))
    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.BROWSING_ITEMS
    assert any("Drumstick" in title for _, title in bot.rows())


async def test_cancel_at_summary_releases_the_slot(session, outlet, menu, bot):
    await _sign_up(session, bot)
    await _add_drumstick(session, bot)
    await handle_event(session, reply("cart:checkout"))
    await handle_event(session, pin(*NEARBY))
    await handle_event(session, text("12 Maple Street, Edison"))
    await handle_event(session, text("skip"))
    await handle_event(session, text("skip"))
    await handle_event(session, text("same"))

    await handle_event(session, reply(bot.row_id("slot:")))
    bot.clear()

    await handle_event(session, reply("summary:cancel"))
    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.COMPLETED
    assert "cancelled" in bot.last().body.lower()

    from sqlalchemy import select

    from app.db.models import SlotHold, SlotHoldStatus

    holds = await session.execute(select(SlotHold))
    assert all(h.status == SlotHoldStatus.RELEASED for h in holds.scalars())


async def test_native_catalogue_cart_is_adopted(session, outlet, menu, bot):
    """If the Meta catalogue is ever enabled, an incoming cart still works."""
    await _sign_up(session, bot)
    await handle_event(session, native_cart((DRUMSTICK, 2), (RASAM, 1)))

    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.CART_REVIEW
    cart = conversation.context["cart"]
    assert {line["retailer_id"] for line in cart} == {DRUMSTICK, RASAM}


# --- template buttons --------------------------------------------------------
async def test_a_template_quick_reply_button_starts_the_flow(session, outlet, menu, bot):
    """A marketing template's "Order Now" button must work like typing it.

    The tap arrives as a button payload, not as text, so the global-keyword
    check has to read `choice` rather than `text`.
    """
    await _sign_up(session, bot)
    bot.clear()

    await handle_event(session, reply("ORDER"))

    _, conversation = await _state(session)
    assert conversation.step != ConversationStep.START
    assert "did not understand" not in bot.last().body.lower()


async def test_a_tapped_button_works_even_mid_address_capture(session, outlet, menu, bot):
    """Typed words are ambiguous during free-text capture; a tap is not.

    Someone giving an address on "Order Street" must not be hijacked, but a
    deliberate button press should still be honoured.
    """
    await _sign_up(session, bot)
    await _add_drumstick(session, bot)
    await handle_event(session, reply("cart:checkout"))
    await handle_event(session, pin(*NEARBY))

    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.AWAIT_ADDRESS

    # Typed: treated as the address, not as a keyword.
    await handle_event(session, text("12 Order Street"))
    customer, _ = await _state(session)
    assert customer.address_line1 == "12 Order Street"


async def test_typed_order_also_starts_the_flow(session, outlet, menu, bot):
    await _sign_up(session, bot)
    bot.clear()

    await handle_event(session, text("Order"))

    assert "did not understand" not in bot.last().body.lower()


# --- outreach to somebody we do not know yet ---------------------------------
async def test_welcome_to_an_unknown_number_then_order_now_asks_for_the_name(
        session, outlet, menu, bot, monkeypatch):
    """The client's case: a lead that does not exist yet.

    We greet them as "there", and the moment they tap Order Now the flow must
    ask who they are - otherwise every later message has no name to use.
    """
    from app.services import outreach

    async def noop(customer):
        return None

    monkeypatch.setattr(outreach, "ensure_record", noop)
    customer, created = await outreach.send_welcome(session, PHONE)
    assert created is True
    assert bot.last().payload["values"] == ["there"]
    bot.clear()

    # They tap Order Now; the button sends its own label back to us.
    await handle_event(session, reply("Order Now"))

    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.AWAIT_NAME
    assert "full name" in bot.last().body.lower()


async def test_the_name_given_after_outreach_is_used_from_then_on(
        session, outlet, menu, bot, monkeypatch):
    from app.services import outreach

    async def noop(customer):
        return None

    monkeypatch.setattr(outreach, "ensure_record", noop)
    await outreach.send_welcome(session, PHONE)
    await handle_event(session, reply("Order Now"))
    await handle_event(session, text("Asha Menon"))

    customer, _ = await _state(session)
    assert customer.name == "Asha Menon"
    assert customer.greeting_name == "Asha Menon"
    assert "Asha Menon" in bot.last().body


# --- messages the client switched off ------------------------------------------
async def test_the_payment_reminder_is_not_sent_when_switched_off(
        session, outlet, menu, bot, monkeypatch):
    """SEND_PAYMENT_REMINDER=false. The nudge goes, the expiry stays."""
    from app.core.config import settings
    from app.db.models import Order, PaymentStatus
    from app.services import payments

    monkeypatch.setattr(settings, "send_payment_reminder", False)

    order = Order(customer_id=None, order_number="SHO-TEST-1",
                  payment_status=PaymentStatus.LINK_SENT)
    customer = type("C", (), {"whatsapp_number": "1", "greeting_name": "Asha"})()

    bot.clear()
    sent = await payments.send_reminder(session, order, customer)

    assert sent is False
    assert bot.sent == []
    assert order.reminder_sent_at is None


async def test_the_payment_reminder_still_works_when_switched_on(
        session, outlet, menu, bot, monkeypatch):
    from app.core.config import settings
    from app.db.models import Order, PaymentStatus
    from app.services import payments

    monkeypatch.setattr(settings, "send_payment_reminder", True)

    order = Order(customer_id=None, order_number="SHO-TEST-2",
                  payment_status=PaymentStatus.LINK_SENT, slot_label="Wed 7-8 PM")
    customer = type("C", (), {"whatsapp_number": "1", "greeting_name": "Asha"})()

    bot.clear()
    sent = await payments.send_reminder(session, order, customer)

    assert sent is True
    assert bot.last().body == tpl.PAYMENT_REMINDER.name


# --- the menu link is a button, not a bare URL ---------------------------------
def _with_signed_links(monkeypatch):
    """Ordering links need a signing secret; without one the bot correctly
    falls back to chat browsing, which is a different path."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "admin_session_secret", "test-signing-secret")
    monkeypatch.setattr(settings, "public_base_url", "https://shero.test")
    monkeypatch.setattr(settings, "order_mode", "web")


async def test_the_menu_link_is_one_message_with_both_buttons(
        session, outlet, menu, bot, monkeypatch):
    """menu_link: View Menu and Get new link together, in a single message."""
    _with_signed_links(monkeypatch)
    await _sign_up(session, bot)
    bot.clear()

    await handle_event(session, reply("menu:order"))

    assert len(bot.sent) == 1
    sent = bot.last()
    assert sent.kind == "template" and sent.body == "menu_link"
    # The button parameter is the signed token alone - the approved template
    # already holds https://<host>/order/.
    assert "http" not in sent.payload["button_value"]
    assert "." in sent.payload["button_value"]


async def test_until_the_template_is_approved_a_plain_link_message_goes(
        session, outlet, menu, bot, monkeypatch):
    """Still one message: View Menu only, and the footer says to type new link."""
    from app.core.exceptions import IntegrationError

    async def not_approved(*args, **kwargs):
        raise IntegrationError("gallabox", "template not approved")

    _with_signed_links(monkeypatch)
    await _sign_up(session, bot)
    bot.clear()
    monkeypatch.setattr(bot, "send_template", not_approved)

    await handle_event(session, reply("menu:order"))

    assert bot.kinds() == ["cta_url"]
    assert bot.last().payload["label"] == "View Menu"
    assert "/order/" in bot.last().payload["url"]
    assert "http" not in bot.last().body


async def test_a_provider_that_refuses_both_still_gets_the_link(
        session, outlet, menu, bot, monkeypatch):
    """Not every WhatsApp provider passes cta_url through. Nobody is stranded."""
    from app.core.exceptions import IntegrationError

    async def refuse(*args, **kwargs):
        raise IntegrationError("gallabox", "interactive type not supported")

    _with_signed_links(monkeypatch)
    await _sign_up(session, bot)
    bot.clear()
    monkeypatch.setattr(bot, "send_template", refuse)
    monkeypatch.setattr(bot, "send_cta_url", refuse)

    await handle_event(session, reply("menu:order"))

    sent = bot.sent[0]
    assert sent.kind == "text"
    assert "/order/" in sent.body          # the address is spelled out instead


async def test_the_button_label_fits_whatsapps_limit():
    from app.services.conversation import prompts as p
    from app.integrations.gallabox.messages import BUTTON_TITLE_LIMIT

    assert len(p.ORDER_LINK_BUTTON_LABEL) <= BUTTON_TITLE_LIMIT


# --- changing the name from the email step ----------------------------------------
async def test_the_email_question_offers_a_change_name_button(session, outlet, menu, bot):
    await handle_event(session, text("hi"))
    await handle_event(session, text("Asha Menon"))

    sent = bot.last()
    assert sent.kind == "buttons"
    assert "Thanks Asha Menon" in sent.body
    assert sent.payload["buttons"] == [("email:change_name", "Change name")]


async def test_change_name_goes_back_and_the_new_name_replaces_the_old(
        session, outlet, menu, bot):
    """The case that prompted it: a greeting the bot took as a name."""
    await handle_event(session, text("hi"))
    await handle_event(session, text("hello"))          # saved as the name

    await handle_event(session, reply("email:change_name"))
    customer, conversation = await _state(session)
    assert conversation.step == ConversationStep.AWAIT_NAME
    assert "full name" in bot.last().body

    await handle_event(session, text("Asha Menon"))
    customer, conversation = await _state(session)
    assert customer.name == "Asha Menon"
    assert conversation.step == ConversationStep.AWAIT_EMAIL
    assert "Thanks Asha Menon" in bot.last().body


async def test_a_bad_email_still_offers_the_change_name_button(session, outlet, menu, bot):
    await handle_event(session, text("hi"))
    await handle_event(session, text("Asha Menon"))
    await handle_event(session, text("not-an-email"))

    sent = bot.last()
    assert sent.kind == "buttons"
    assert "valid email" in sent.body
    assert sent.payload["buttons"] == [("email:change_name", "Change name")]


async def test_typing_the_words_change_name_is_treated_as_an_email(
        session, outlet, menu, bot):
    """Only the tapped button goes back; typed text at this step is an answer."""
    await handle_event(session, text("hi"))
    await handle_event(session, text("Asha Menon"))
    await handle_event(session, text("change name"))

    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.AWAIT_EMAIL
    assert "valid email" in bot.last().body
