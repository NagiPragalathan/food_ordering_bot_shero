"""Opening a conversation with somebody we know nothing about.

Every other message the bot sends is a reply, so the customer already exists
and has a name. Outreach is the one path where neither is true, and the
failure it guards against is a template rendering "Hi , welcome to Shero".
"""

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Conversation, ConversationStep, Customer
from app.integrations.gallabox.sender import use_sender
from app.services import outreach
from tests.conversation.test_happy_path import FakeGallabox

NEW_NUMBER = "919876500011"


@pytest_asyncio.fixture
async def quiet_crm(monkeypatch):
    """Zoho is not configured in tests; outreach must not depend on it."""
    async def noop(customer):
        return None

    monkeypatch.setattr(outreach, "ensure_lead", noop)


# --- the case the client raised -----------------------------------------------
@pytest.mark.asyncio
async def test_an_unknown_number_gets_a_lead_and_a_neutral_greeting(
        session: AsyncSession, quiet_crm):
    fake = FakeGallabox()
    with use_sender(fake):
        customer, created = await outreach.send_welcome(session, NEW_NUMBER)

    assert created is True
    assert customer.name is None

    sent = fake.last()
    assert sent.kind == "template"
    assert sent.body == "shero_welcome"
    # Not an empty string, and not a name we invented for them.
    assert sent.payload["values"] == ["there"]


@pytest.mark.asyncio
async def test_the_lead_is_stored_so_the_reply_has_somewhere_to_land(
        session: AsyncSession, quiet_crm):
    fake = FakeGallabox()
    with use_sender(fake):
        await outreach.send_welcome(session, NEW_NUMBER)

    found = await session.execute(
        select(Customer).where(Customer.whatsapp_number == NEW_NUMBER))
    stored = found.scalar_one()
    assert stored.lead_source == "WhatsApp"


@pytest.mark.asyncio
async def test_the_conversation_starts_at_the_first_step(
        session: AsyncSession, quiet_crm):
    """So tapping Order Now runs onboarding, which asks for the name."""
    fake = FakeGallabox()
    with use_sender(fake):
        customer, _ = await outreach.send_welcome(session, NEW_NUMBER)

    found = await session.execute(
        select(Conversation).where(Conversation.customer_id == customer.id))
    assert found.scalar_one().step == ConversationStep.START


@pytest.mark.asyncio
async def test_a_known_customer_is_greeted_by_name(
        session: AsyncSession, customer: Customer, quiet_crm):
    fake = FakeGallabox()
    with use_sender(fake):
        _, created = await outreach.send_welcome(session, customer.whatsapp_number)

    assert created is False
    assert fake.last().payload["values"] == ["Asha Menon"]


@pytest.mark.asyncio
async def test_a_blank_name_is_treated_as_no_name(
        session: AsyncSession, customer: Customer, quiet_crm):
    customer.name = "   "
    await session.flush()

    fake = FakeGallabox()
    with use_sender(fake):
        await outreach.send_welcome(session, customer.whatsapp_number)

    assert fake.last().payload["values"] == ["there"]


@pytest.mark.asyncio
async def test_sending_twice_does_not_create_a_second_lead(
        session: AsyncSession, quiet_crm):
    fake = FakeGallabox()
    with use_sender(fake):
        first, created_first = await outreach.send_welcome(session, NEW_NUMBER)
        second, created_second = await outreach.send_welcome(session, NEW_NUMBER)

    assert created_first is True
    assert created_second is False
    assert first.id == second.id


# --- guards --------------------------------------------------------------------
@pytest.mark.asyncio
async def test_nothing_is_sent_while_a_human_has_the_conversation(
        session: AsyncSession, customer: Customer, quiet_crm):
    from app.services import customers as customer_service

    conversation = await customer_service.get_or_create_conversation(session, customer)
    conversation.step = ConversationStep.HANDED_OVER
    await session.flush()

    fake = FakeGallabox()
    with use_sender(fake), pytest.raises(outreach.AlreadyWithAgent):
        await outreach.send_welcome(session, customer.whatsapp_number)

    assert fake.sent == []


@pytest.mark.asyncio
async def test_an_unreachable_crm_does_not_stop_the_greeting(
        session: AsyncSession, monkeypatch):
    from app.core.exceptions import IntegrationError

    async def exploding(customer):
        raise IntegrationError("zoho", "refresh token revoked")

    monkeypatch.setattr(outreach, "ensure_lead", exploding)

    fake = FakeGallabox()
    with use_sender(fake):
        await outreach.send_welcome(session, NEW_NUMBER)

    assert fake.last().body == "shero_welcome"


# --- the shared greeting rule ---------------------------------------------------
def test_greeting_name_falls_back_for_every_template():
    """payment_link and friends greet by name too, from the same property."""
    assert Customer(whatsapp_number="1", name=None).greeting_name == "there"
    assert Customer(whatsapp_number="1", name="").greeting_name == "there"
    assert Customer(whatsapp_number="1", name="  ").greeting_name == "there"
    assert Customer(whatsapp_number="1", name="Asha").greeting_name == "Asha"
