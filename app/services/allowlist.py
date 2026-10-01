"""Which WhatsApp numbers the bot is allowed to answer.

Set on the admin **Bot replies** page, or in the environment:

    BOT_REPLY_MODE=all          answer everyone (production)
    BOT_REPLY_MODE=allowlist    answer only BOT_ALLOWED_NUMBERS

    BOT_ALLOWED_NUMBERS=917401268091|Nagi,14155550123

Each entry is a number with an optional name after "|" (the name is only for
the admin page). A number the bot does not answer is left alone and keeps
reaching the team in Gallabox exactly as if the bot were not there.

With BOT_REPLY_MODE unset the list decides: empty means everyone, so a
missing setting never silently switches the bot off in production.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import settings

ALL, ALLOWLIST = "all", "allowlist"
MODES = (ALL, ALLOWLIST)

# A number given without its country code is matched against the end of the
# incoming one. Ten digits is the shortest national number we would accept that
# way: it is specific enough to mean one phone, where a shorter suffix could
# quietly let a stranger's number through.
MIN_SUFFIX_DIGITS = 10
# What the admin page accepts: a full international number is at most 15
# digits (E.164); fewer than 10 cannot be one phone.
VALID_DIGITS = range(10, 16)


@dataclass(frozen=True)
class Entry:
    number: str        # digits only
    name: str = ""


def _digits(raw: str) -> str:
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def clean_name(raw: str) -> str:
    """A name safe to store inside the list (no separators)."""
    return " ".join((raw or "").replace("|", " ").replace(",", " ").split())[:60]


def parse_entries(raw: str) -> list[Entry]:
    """Entries from a comma or newline separated list of `number|name`, in
    order, without duplicate numbers (the first name wins)."""
    seen: dict[str, Entry] = {}
    for part in (raw or "").replace("\n", ",").replace(";", ",").split(","):
        number, _, name = part.partition("|")
        digits = _digits(number)
        if digits and digits not in seen:
            seen[digits] = Entry(digits, clean_name(name))
    return list(seen.values())


def format_entries(items: list[Entry]) -> str:
    """The stored form read back by `parse_entries`."""
    return ",".join(f"{e.number}|{e.name}" if e.name else e.number for e in items)


def parse_numbers(raw: str) -> list[str]:
    """Just the numbers of `parse_entries`."""
    return [entry.number for entry in parse_entries(raw)]


def is_valid(number: str) -> bool:
    return len(_digits(number)) in VALID_DIGITS


def entries() -> list[Entry]:
    """The whitelist as configured."""
    return parse_entries(settings.bot_allowed_numbers)


def allowed_numbers() -> set[str]:
    """The whitelisted numbers, digits only."""
    return {entry.number for entry in entries()}


def mode() -> str:
    """`all` or `allowlist`, as set; unset falls back to the list."""
    chosen = (settings.bot_reply_mode or "").strip().lower()
    if chosen in MODES:
        return chosen
    return ALLOWLIST if allowed_numbers() else ALL


def is_restricted() -> bool:
    """True while the bot answers only the whitelist."""
    return mode() == ALLOWLIST


def permits(number: str) -> bool:
    """May the bot talk to this number?

    Matches the full international number, or - for a whitelisted number of
    at least ten digits - the same number written without its country code,
    so `7401268091` and `917401268091` both mean the same phone.
    """
    if mode() == ALL:
        return True
    return match(number) is not None


def match(number: str) -> Entry | None:
    """The whitelist entry this number counts as, if any (whatever the mode)."""
    incoming = _digits(number)
    if not incoming:
        return None
    for entry in entries():
        if incoming == entry.number or (len(entry.number) >= MIN_SUFFIX_DIGITS
                                        and incoming.endswith(entry.number)):
            return entry
    return None
