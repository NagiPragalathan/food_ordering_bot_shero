"""The welcome menu: what a new chat gets first, in keyword mode.

Shero's WhatsApp number also runs Gallabox bot flows (Party Order, Sweet &
Snacks). Gallabox cannot start one of its flows from outside, and its own
"any keyword" card answered every message - including a customer typing
their name to this bot. So this bot sends the menu instead, and each of its
buttons either starts this bot's ordering or is left to a Gallabox flow:

  * The menu goes to a message nothing else answers (no trigger keyword, not
    part-way through an order) when the chat is new: neither the menu, this
    bot nor a Gallabox flow has been in it for QUIET_HOURS.
  * A bot button starts ordering, like a trigger keyword, with its own
    welcome reply (reply_triggers.REPLIES).
  * A Gallabox button gets no reply here. In Gallabox that flow has the
    button's label as its keyword, so the tap starts it there. This bot then
    stays out of the chat for QUIET_HOURS - the flow's own buttons and
    questions are Gallabox's - unless the customer types a trigger keyword
    or taps a bot button.

Only in keyword mode ("Reply to any message" off): with it on, the bot
answers everything itself. Stored as one JSON setting, BOT_WELCOME_MENU,
from the admin Bot replies page.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ConfigurationError, IntegrationError
from app.core.logging import get_logger
from app.db.models import WelcomeMenuChat
from app.integrations.gallabox import messages as m
from app.integrations.gallabox.sender import current_sender
from app.services import reply_triggers as rt

log = get_logger(__name__)

GALLABOX = "gallabox"
# Where a button goes: this bot (with which welcome messages), or Gallabox.
GOES_TO = {rt.BOTH: "Ordering bot: both messages",
           rt.ORDER_NOW: "Ordering bot: only Order Now",
           rt.WHATSAPP: "Ordering bot: only WhatsApp",
           GALLABOX: "A Gallabox flow"}

MAX_BUTTONS = 3                      # WhatsApp's limit for reply buttons
LABEL_LIMIT = m.BUTTON_TITLE_LIMIT   # 20 characters
TEXT_LIMIT = 1024
QUIET_HOURS = 24                     # WhatsApp's own conversation window

# What Gallabox's Main Card said, so switching over changes nothing visible.
DEFAULT_TEXT = "Hi!\n\nThanks for reaching out to Shero.\nHow can we help you today?"

# What decide() asks the webhook to do (Decision.action).
SEND = "send_menu"
LEFT_TO_GALLABOX = "gallabox"


@dataclass(frozen=True)
class MenuButton:
    label: str
    goes_to: str = rt.BOTH

    @property
    def to_gallabox(self) -> bool:
        return self.goes_to == GALLABOX


DEFAULT_BUTTONS = (MenuButton("Party Order", GALLABOX), MenuButton("Sweet & Snacks", GALLABOX),
                   MenuButton("Single Meal Order", rt.WHATSAPP))


@dataclass(frozen=True)
class Menu:
    enabled: bool = False
    text: str = DEFAULT_TEXT
    buttons: tuple[MenuButton, ...] = field(default=DEFAULT_BUTTONS)

    def button(self, said: str) -> MenuButton | None:
        """The button whose label is the whole message (typed or tapped)."""
        words = rt.normalise(said)
        return next((b for b in self.buttons if words and rt.normalise(b.label) == words), None)


# --- the stored menu ---------------------------------------------------------
def clean_label(raw: str) -> str:
    return " ".join((raw or "").split())


def parse(raw: str) -> Menu:
    """The stored JSON as a menu; the defaults for anything missing or unreadable."""
    try:
        data = json.loads(raw) if (raw or "").strip() else {}
    except ValueError:
        log.warning("welcome_menu_unreadable")
        data = {}
    if not isinstance(data, dict):
        data = {}
    buttons = tuple(MenuButton(clean_label(str(b.get("label") or "")), str(b.get("goes_to") or ""))
                    for b in data.get("buttons") or [] if isinstance(b, dict))
    buttons = tuple(b for b in buttons if b.label and b.goes_to in GOES_TO)[:MAX_BUTTONS]
    text = str(data.get("text") or "").strip()
    return Menu(enabled=bool(data.get("enabled")) and bool(buttons),
                text=text or DEFAULT_TEXT, buttons=buttons or DEFAULT_BUTTONS)


def dump(menu: Menu) -> str:
    return json.dumps({"enabled": menu.enabled, "text": menu.text,
                       "buttons": [{"label": b.label, "goes_to": b.goes_to} for b in menu.buttons]},
                      ensure_ascii=False)


def problems(menu: Menu) -> list[str]:
    """Why this menu can't be saved: WhatsApp's limits, and labels told apart."""
    found = []
    if not menu.text.strip():
        found.append("Write the menu message.")
    if len(menu.text) > TEXT_LIMIT:
        found.append(f"The message is longer than {TEXT_LIMIT} characters.")
    if not menu.buttons:
        found.append("Add at least one button.")
    if len(menu.buttons) > MAX_BUTTONS:
        found.append(f"WhatsApp allows at most {MAX_BUTTONS} buttons.")
    for b in menu.buttons:
        if len(b.label) > LABEL_LIMIT:
            found.append(f"“{b.label}” is longer than {LABEL_LIMIT} characters.")
        if not rt.normalise(b.label):
            found.append(f"“{b.label}” needs at least one letter or number.")
        if b.goes_to not in GOES_TO:
            found.append(f"Choose where “{b.label}” goes.")
    labels = [rt.normalise(b.label) for b in menu.buttons]
    if len(set(labels)) != len(labels):
        found.append("Each button needs a different label.")
    return found


def load() -> Menu:
    return parse(settings.bot_welcome_menu)


def active() -> Menu | None:
    """The menu, when it is switched on and the bot is in keyword mode."""
    menu = load()
    return menu if menu.enabled and rt.is_keyword_mode() else None


# --- each chat ---------------------------------------------------------------
def _aware(moment: datetime | None) -> datetime | None:
    return moment if moment is None or moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _recent(moment: datetime | None, now: datetime) -> bool:
    moment = _aware(moment)
    return moment is not None and now - moment < timedelta(hours=QUIET_HOURS)


async def with_gallabox(session: AsyncSession, number: str, now: datetime) -> bool:
    """Did they pick a Gallabox flow lately? Then the chat is that flow's."""
    row = await session.get(WelcomeMenuChat, number)
    return row is not None and _recent(row.gallabox_at, now)


async def due(session: AsyncSession, number: str, now: datetime,
              bot_last_active: datetime | None) -> bool:
    """Is this a new chat: no menu, Gallabox flow or bot in it lately?"""
    if _recent(bot_last_active, now):
        return False
    row = await session.get(WelcomeMenuChat, number)
    return row is None or not (_recent(row.menu_sent_at, now) or _recent(row.gallabox_at, now))


async def _chat(session: AsyncSession, number: str) -> WelcomeMenuChat:
    row = await session.get(WelcomeMenuChat, number)
    if row is None:
        row = WelcomeMenuChat(whatsapp_number=number)
        session.add(row)
    return row


async def send(session: AsyncSession, number: str, now: datetime | None = None) -> bool:
    """Send the menu and note it. A failed send is not noted, so the next
    message tries again."""
    menu = load()
    buttons = [m.Button(b.label, b.label) for b in menu.buttons]   # the id is the label:
    try:                                                           # Gallabox's keyword sees either
        await current_sender().send_buttons(number, menu.text, buttons)
    except (IntegrationError, ConfigurationError) as exc:
        log.warning("welcome_menu_failed", number_tail=number[-4:], error=str(exc))
        return False
    (await _chat(session, number)).menu_sent_at = now or datetime.now(timezone.utc)
    await session.flush()
    log.info("welcome_menu_sent", number_tail=number[-4:])
    return True


async def left_to_gallabox(session: AsyncSession, number: str, label: str,
                           now: datetime | None = None) -> None:
    """They picked a Gallabox flow: keep out of the chat while it runs."""
    row = await _chat(session, number)
    row.gallabox_flow, row.gallabox_at = label[:40], now or datetime.now(timezone.utc)
    await session.flush()
    log.info("welcome_menu_gallabox", number_tail=number[-4:], flow=label)


async def back_to_bot(session: AsyncSession, number: str) -> None:
    """They came back to ordering (a trigger, a bot button): the bot is theirs again."""
    row = await session.get(WelcomeMenuChat, number)
    if row is not None and row.gallabox_at is not None:
        row.gallabox_flow = row.gallabox_at = None
        await session.flush()
