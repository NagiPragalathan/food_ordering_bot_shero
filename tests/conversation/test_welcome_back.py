"""A returning customer's "hi" is greeted by name, with the two ways to order:
Order Now (a link button to the website) and Continue on WhatsApp."""

from __future__ import annotations

from app.integrations.gallabox.templates import CONTINUE_ON_WHATSAPP
from app.services.conversation.engine import handle_event
from tests.conversation.test_happy_path import (  # noqa: F401 - `bot` is a fixture
    _sign_up,
    bot,
    reply,
    text,
)


async def test_a_known_customer_saying_hi_is_welcomed_back_by_name(
        session, outlet, menu, bot):
    await _sign_up(session, bot)
    bot.clear()

    await handle_event(session, text("hi"))

    link, choice = bot.sent                        # Order Now, then Continue
    assert link.kind == "cta_url"
    assert link.body.startswith("Welcome back to Shero Home Food, Asha Menon!")
    assert "What would you like to do?" not in link.body
    assert link.payload == {"url": "https://www.shero.us/", "label": "Order Now"}
    assert choice.kind == "buttons"
    assert choice.payload["buttons"] == [("Continue on WhatsApp", "Continue on WhatsApp")]


async def test_menu_and_hello_greet_the_same_way(session, outlet, menu, bot):
    await _sign_up(session, bot)

    for word in ("menu", "hello"):
        bot.clear()
        await handle_event(session, text(word))
        assert bot.sent[0].body.startswith("Welcome back to Shero Home Food")


async def test_continue_on_whatsapp_goes_on_to_ordering(session, outlet, menu, bot):
    await _sign_up(session, bot)
    await handle_event(session, text("hi"))
    bot.clear()

    await handle_event(session, reply(CONTINUE_ON_WHATSAPP))

    assert bot.sent, "the tap must be answered"
    assert all(m.kind != "cta_url" for m in bot.sent)       # no second greeting


async def test_continue_on_whatsapp_from_an_old_message_still_works(
        session, outlet, menu, bot):
    """Tapped later, from another step: straight on to ordering, not a greeting."""
    await _sign_up(session, bot)
    await handle_event(session, text("hi"))
    await handle_event(session, reply(CONTINUE_ON_WHATSAPP))
    first = (bot.last().kind, bot.last().body)
    bot.clear()

    await handle_event(session, reply(CONTINUE_ON_WHATSAPP))

    assert (bot.last().kind, bot.last().body) == first
    assert all(m.kind != "cta_url" for m in bot.sent)


async def test_a_new_customer_is_still_asked_for_their_name(session, outlet, menu, bot):
    await handle_event(session, text("hi"))
    assert "full name" in bot.last().body
