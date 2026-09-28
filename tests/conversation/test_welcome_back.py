"""A returning customer's "hi" is greeted by name, in one message with the menu."""

from __future__ import annotations

from app.services.conversation import prompts as p
from app.services.conversation.engine import handle_event
from tests.conversation.test_happy_path import (  # noqa: F401 - `bot` is a fixture
    _sign_up,
    bot,
    text,
)


async def test_a_known_customer_saying_hi_is_welcomed_back_by_name(
        session, outlet, menu, bot):
    await _sign_up(session, bot)
    bot.clear()

    await handle_event(session, text("hi"))

    assert len(bot.sent) == 1                      # greeting and menu together
    sent = bot.last()
    assert sent.kind == "buttons"
    assert sent.body.startswith("Welcome back to Shero Home Food, Asha Menon!")
    assert p.MAIN_MENU in sent.body
    assert sent.payload["buttons"] == [(p.MENU_ORDER, p.BTN_ORDER_NOW)]


async def test_menu_and_hello_greet_the_same_way(session, outlet, menu, bot):
    await _sign_up(session, bot)

    for word in ("menu", "hello"):
        bot.clear()
        await handle_event(session, text(word))
        assert bot.last().body.startswith("Welcome back to Shero Home Food")


async def test_a_new_customer_is_still_asked_for_their_name(session, outlet, menu, bot):
    await handle_event(session, text("hi"))
    assert "full name" in bot.last().body
