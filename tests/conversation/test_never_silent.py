"""Every message a customer sends gets an answer.

Silence is indistinguishable from a broken bot, so the engine backs up the
step handlers: media it cannot read is explained, and a handler that sent
nothing is followed by a nudge. The one deliberate silence is a chat handed
to a human agent.
"""

from app.db.models import ConversationStep
from app.schemas.inbound import InboundKind
from app.services.conversation import engine
from app.services.conversation import prompts as p
from app.services.conversation.engine import handle_event
from tests.conversation.test_happy_path import (  # noqa: F401 - `bot` is a fixture
    _event,
    _sign_up,
    _state,
    bot,
    reply,
    text,
)


def voice_note():
    return _event(kind=InboundKind.MEDIA)


async def test_a_voice_note_mid_flow_is_explained_not_ignored(session, outlet, menu, bot):
    await handle_event(session, text("hi"))
    await handle_event(session, text("Asha Menon"))       # now at the email step
    bot.clear()

    await handle_event(session, voice_note())

    sent = bot.last()
    assert sent.kind == "text"
    assert "voice notes" in sent.body
    # A free-text step: "menu" would be taken as the answer, so it is not offered.
    assert p.TYPE_YOUR_ANSWER in sent.body
    assert "'menu'" not in sent.body
    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.AWAIT_EMAIL


async def test_a_photo_at_a_button_step_points_at_the_buttons(session, outlet, menu, bot):
    await _sign_up(session, bot)                           # now at the main menu
    bot.clear()

    await handle_event(session, voice_note())

    assert p.MEDIA_HINT in bot.last().body
    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.MAIN_MENU


async def test_a_voice_note_as_the_first_message_gets_the_welcome(session, outlet, menu, bot):
    await handle_event(session, voice_note())

    assert "what is your full name" in bot.last().body


async def test_a_handler_that_says_nothing_is_backed_up_with_a_nudge(
        session, outlet, menu, bot, monkeypatch):
    await _sign_up(session, bot)
    bot.clear()

    async def says_nothing(ctx):
        return None

    monkeypatch.setitem(engine.STEP_HANDLERS, ConversationStep.MAIN_MENU, says_nothing)
    await handle_event(session, text("something unexpected"))

    assert [m.body for m in bot.sent] == [p.FALLBACK]


async def test_the_nudge_at_a_free_text_step_does_not_suggest_menu(
        session, outlet, menu, bot, monkeypatch):
    await handle_event(session, text("hi"))                # now at the name step
    bot.clear()

    async def says_nothing(ctx):
        return None

    monkeypatch.setitem(engine.STEP_HANDLERS, ConversationStep.AWAIT_NAME, says_nothing)
    await handle_event(session, text("???"))

    assert [m.body for m in bot.sent] == [p.UNMATCHED_FREE_TEXT]


async def test_no_extra_nudge_when_the_handler_already_replied(session, outlet, menu, bot):
    await handle_event(session, text("hi"))
    bot.clear()

    await handle_event(session, text("Asha Menon"))

    assert len(bot.sent) == 1                              # just the email question


async def test_a_chat_with_a_human_agent_stays_quiet(session, outlet, menu, bot):
    await _sign_up(session, bot)
    await handle_event(session, reply("menu:talk"))
    bot.clear()

    await handle_event(session, text("are you there?"))
    await handle_event(session, voice_note())

    assert bot.sent == []
