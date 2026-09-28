"""Which WhatsApp numbers the bot is allowed to answer.

For testing on a live business number. Set `BOT_ALLOWED_NUMBERS` to your own
number and the bot answers only you; every other customer is left alone and
keeps reaching your team in Gallabox exactly as if the bot were not there.

Leave it empty and the bot answers everyone, which is production behaviour.
Empty means "everyone" rather than "no one" on purpose: a missing setting must
never silently switch the bot off in production.

    BOT_ALLOWED_NUMBERS=917401268091
    BOT_ALLOWED_NUMBERS=917401268091,14155550123
"""

from __future__ import annotations

from app.core.config import settings

# A number given without its country code is matched against the end of the
# incoming one. Ten digits is the shortest national number we would accept that
# way: it is specific enough to mean one phone, where a shorter suffix could
# quietly let a stranger's number through.
MIN_SUFFIX_DIGITS = 10


def _digits(raw: str) -> str:
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def allowed_numbers() -> set[str]:
    """The configured numbers, digits only. Empty set means everyone."""
    return {d for d in (_digits(part) for part in settings.bot_allowed_numbers.split(","))
            if d}


def is_restricted() -> bool:
    """True while the bot is limited to a test list."""
    return bool(allowed_numbers())


def permits(number: str) -> bool:
    """May the bot talk to this number?

    Matches the full international number, or - for a configured number of
    at least ten digits - the same number written without its country code,
    so `7401268091` and `917401268091` both mean the same phone.
    """
    allowed = allowed_numbers()
    if not allowed:
        return True

    incoming = _digits(number)
    if not incoming:
        return False

    return any(
        incoming == entry
        or (len(entry) >= MIN_SUFFIX_DIGITS and incoming.endswith(entry))
        for entry in allowed
    )
