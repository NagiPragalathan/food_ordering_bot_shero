"""Getting a fresh menu link: the chat button, the typed words, the expired page.

"Get new link" is a quick reply on the menu_link template, which comes back
as its own label. The expired page opens the chat with "New menu link"
already typed, which the bot answers the same way.
"""

from __future__ import annotations

from urllib.parse import unquote

from app.core.config import settings
from app.integrations.gallabox.messages import BUTTON_TITLE_LIMIT
from app.services import order_link
from app.services.conversation import prompts as p
from app.services.conversation.engine import handle_event
from tests.conversation.test_happy_path import (  # noqa: F401 - `bot` is a fixture
    _sign_up,
    _with_signed_links,
    bot,
    reply,
    text,
)


def menu_link_sent(bot) -> bool:
    return bot.sent and bot.sent[0].kind == "template" and bot.sent[0].body == "menu_link_v2"


async def test_the_get_new_link_quick_reply_sends_a_fresh_link(
        session, outlet, menu, bot, monkeypatch):
    _with_signed_links(monkeypatch)
    await _sign_up(session, bot)
    await handle_event(session, reply(p.MENU_ORDER))
    bot.clear()

    # A template quick reply arrives carrying its label.
    await handle_event(session, reply(p.BTN_NEW_LINK))

    assert menu_link_sent(bot)


async def test_an_old_separate_get_new_link_button_still_works(
        session, outlet, menu, bot, monkeypatch):
    """Buttons from before the template existed are still in people's chats."""
    _with_signed_links(monkeypatch)
    await _sign_up(session, bot)
    bot.clear()

    await handle_event(session, reply(p.NEW_LINK))

    assert menu_link_sent(bot)


async def test_the_expired_pages_message_also_gets_a_fresh_link(
        session, outlet, menu, bot, monkeypatch):
    _with_signed_links(monkeypatch)
    await _sign_up(session, bot)
    bot.clear()

    await handle_event(session, text(p.NEW_LINK_REQUEST))      # "New menu link"

    assert menu_link_sent(bot)


async def test_typing_new_link_works_too(session, outlet, menu, bot, monkeypatch):
    """What the plain link message's footer tells people to type."""
    _with_signed_links(monkeypatch)
    await _sign_up(session, bot)
    bot.clear()

    await handle_event(session, text("new link"))

    assert menu_link_sent(bot)


async def test_a_stranger_asking_for_a_link_is_onboarded_first(
        session, outlet, menu, bot, monkeypatch):
    """No name or email yet: the link waits until they are captured."""
    _with_signed_links(monkeypatch)
    await handle_event(session, reply(p.NEW_LINK))

    assert "full name" in bot.last().body


def test_the_quick_reply_label_is_what_the_bot_listens_for():
    from app.integrations.gallabox import templates as tpl

    assert tpl.MENU_LINK.quick_replies == (p.BTN_NEW_LINK,)
    assert p.BTN_NEW_LINK.lower() in p.NEW_LINK_KEYWORDS


def test_the_footer_fits_whatsapps_limit():
    assert len(p.ORDER_LINK_FOOTER) <= 60
    assert "new link" in p.ORDER_LINK_FOOTER


def test_the_button_label_fits_whatsapps_limit():
    assert len(p.BTN_NEW_LINK) <= BUTTON_TITLE_LIMIT


# --- the expired page and the page's JSON routes -----------------------------------
def test_the_new_link_button_opens_the_chat_with_the_words_typed(monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_business_number", "14438011011")
    url = order_link.new_link_whatsapp_url()

    assert url.startswith("https://wa.me/14438011011?text=")
    typed = unquote(url.split("text=", 1)[1])
    assert typed.lower() in p.NEW_LINK_KEYWORDS


def test_no_business_number_means_no_new_link_button(monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_business_number", "")
    assert order_link.new_link_whatsapp_url() == ""


def test_a_dead_link_tells_the_page_to_offer_a_new_one(monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_business_number", "14438011011")
    answer = order_link.dead_link_response()

    assert answer["ok"] is False
    assert answer["expired"] is True
    assert answer["new_link_url"].startswith("https://wa.me/14438011011")
