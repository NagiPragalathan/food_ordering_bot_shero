"""Keyword triggers: whether a WhatsApp message should start the bot.

Set on the admin **Bot replies** page, which stores two values in the
database (never read from .env):

    BOT_REPLY_TRIGGER     any | keywords
    BOT_TRIGGER_KEYWORDS  [{"keyword": "order food", "match": "contains",
                            "reply": "both"}, ...]

With "any" (the default, and what nothing saved means) the bot answers every
message, as it always has. With "keywords" a chat only *starts* the bot when
the message matches a trigger. Once it has started, the customer is mid-order
and has to be able to type an address or an email, so the bot keeps answering
everything until the order is finished or the chat goes quiet.

Always answered in keyword mode, trigger or not:
  * a tap on one of the bot's own buttons (it is a reply to the bot)
  * the bot's own prefilled messages ("New menu link" from an expired page)
  * a customer part-way through an order, active in the last ACTIVE_HOURS

Each keyword also says how the bot answers it (REPLIES): the welcome's two
messages, only the Order Now (website) one, or only Continue on WhatsApp.
The choice is kept on the conversation, so a new customer who first gives
their name and email still gets it (onboarding.show_main_menu).

Anything else is left alone - no customer record, no reply, no Zoho lead - and
reaches the team in Gallabox exactly as if the bot were not there.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Conversation, ConversationStep
from app.schemas.inbound import InboundEvent
from app.services.conversation import prompts as p
from app.services.customers import get_customer

log = get_logger(__name__)

ANY, KEYWORDS = "any", "keywords"
MODES = (ANY, KEYWORDS)

EXACT, CONTAINS, STARTS = "exact", "contains", "starts"
# Shown on the admin page, in this order.
MATCHES = {EXACT: "Exact message", CONTAINS: "Contains the word", STARTS: "Starts with"}

# How the bot answers a trigger: which of the welcome's messages it sends.
BOTH, ORDER_NOW, WHATSAPP = "both", "order_now", "whatsapp"
REPLIES = {BOTH: "Both messages", ORDER_NOW: "Only Order Now", WHATSAPP: "Only WhatsApp"}

MAX_KEYWORDS = 50
MAX_KEYWORD_LENGTH = 60

# How long a half-finished order keeps the bot answering without a trigger.
# Matches WhatsApp's own 24-hour customer service window.
ACTIVE_HOURS = 24

# Steps where the bot's flow is not running: a message here needs a trigger.
IDLE_STEPS = {ConversationStep.START, ConversationStep.COMPLETED}

# Messages the bot itself puts in the customer's mouth (a wa.me link's text,
# a button's label), so they must work whatever triggers are set.
BUILT_IN = p.NEW_LINK_KEYWORDS | p.CONTINUE_KEYWORDS


@dataclass(frozen=True)
class Trigger:
    keyword: str        # as typed on the admin page
    match: str = EXACT
    reply: str = BOTH


@dataclass(frozen=True)
class Decision:
    answer: bool
    reason: str
    # A trigger on a chat the bot is not in the middle of: start over rather
    # than feed "order food" to whatever question an old chat stopped at.
    fresh_start: bool = False
    # The matched trigger's reply (REPLIES); "" when no trigger matched.
    reply: str = ""


# --- the stored list ---------------------------------------------------------
def normalise(text: str) -> str:
    """Lower-case words only: "Hi!!", " hi " and "HI 👋" are all "hi"."""
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


def clean_keyword(raw: str) -> str:
    """A keyword as stored: trimmed, single-spaced, at most 60 characters."""
    return " ".join((raw or "").split())[:MAX_KEYWORD_LENGTH]


def parse(raw: str) -> list[Trigger]:
    """The stored JSON as triggers, skipping anything unreadable and repeats."""
    try:
        rows = json.loads(raw) if (raw or "").strip() else []
    except ValueError:
        log.warning("trigger_keywords_unreadable")
        return []
    if not isinstance(rows, list):
        return []
    seen: dict[tuple[str, str], Trigger] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        keyword = clean_keyword(str(row.get("keyword") or ""))
        match = str(row.get("match") or EXACT)
        reply = str(row.get("reply") or BOTH)       # saved before replies existed
        if match not in MATCHES or reply not in REPLIES or not normalise(keyword):
            continue
        seen.setdefault((normalise(keyword), match), Trigger(keyword, match, reply))
    return list(seen.values())[:MAX_KEYWORDS]


def dump(triggers: list[Trigger]) -> str:
    """The stored form read back by `parse`."""
    return json.dumps([{"keyword": t.keyword, "match": t.match, "reply": t.reply}
                       for t in triggers],
                      ensure_ascii=False)


def triggers() -> list[Trigger]:
    return parse(settings.bot_trigger_keywords)


def mode() -> str:
    """`any` or `keywords`. Unset, or keywords with an empty list, is `any`:
    a missing setting must never silently switch the bot off."""
    chosen = (settings.bot_reply_trigger or "").strip().lower()
    return KEYWORDS if chosen == KEYWORDS and triggers() else ANY


def is_keyword_mode() -> bool:
    return mode() == KEYWORDS


# --- matching ----------------------------------------------------------------
def matches(trigger: Trigger, message: str) -> bool:
    """Whole words only, so "hi" is not found inside "this"."""
    word, text = normalise(trigger.keyword), normalise(message)
    if not word or not text:
        return False
    if trigger.match == EXACT:
        return text == word
    if trigger.match == STARTS:
        return text == word or text.startswith(word + " ")
    return f" {word} " in f" {text} "


def matching(message: str) -> Trigger | None:
    """The first trigger this message matches, if any."""
    return next((t for t in triggers() if matches(t, message)), None)


# --- the decision ------------------------------------------------------------
async def decide(session: AsyncSession, event: InboundEvent,
                 now: datetime | None = None) -> Decision:
    """Should the bot answer this message? Reads, never creates, records."""
    if mode() == ANY or not event.is_actionable:
        return Decision(True, "any message")
    if event.reply_id:
        return Decision(True, "button tap")

    message = event.text or ""
    if normalise(message) in BUILT_IN:
        return Decision(True, "bot link")

    in_flow = await _in_flow(session, event.whatsapp_number,
                             now or datetime.now(timezone.utc))
    trigger = matching(message)
    if trigger:
        return Decision(True, "trigger", fresh_start=not in_flow, reply=trigger.reply)
    if in_flow:
        return Decision(True, "order in progress")
    return Decision(False, "no trigger")


async def _in_flow(session: AsyncSession, number: str, now: datetime) -> bool:
    """Is this customer part-way through an order, recently?"""
    customer = await get_customer(session, number)
    if customer is None:
        return False
    conversation = (await session.execute(select(Conversation).where(
        Conversation.customer_id == customer.id))).scalar_one_or_none()
    if conversation is None:
        return False
    try:
        step = ConversationStep(conversation.step)
    except ValueError:
        return False
    # A chat handed to a person is the person's: the bot stays out of it.
    if step in IDLE_STEPS or step is ConversationStep.HANDED_OVER:
        return False
    last = _latest(conversation.last_message_at, conversation.updated_at)
    return last is not None and now - last <= timedelta(hours=ACTIVE_HOURS)


def _latest(*moments: datetime | None) -> datetime | None:
    known = [m if m.tzinfo else m.replace(tzinfo=timezone.utc) for m in moments if m]
    return max(known) if known else None


def welcome_reply(saved: str | None) -> str:
    """Which welcome messages to send, given the reply kept on the chat.

    Only while keyword mode is on: switched back to Reply to any message,
    everyone gets both again, whatever an earlier trigger said.
    """
    if is_keyword_mode() and saved in REPLIES:
        return saved
    return BOTH
