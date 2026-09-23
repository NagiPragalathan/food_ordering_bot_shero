"""Input validation for the fields the bot collects.

Each returns a cleaned value or None, so handlers read as:

    email = clean_email(event.text)
    if email is None:
        ... re-ask ...

which is exactly the spec's "Re-ask if format is invalid" behaviour.
"""

from __future__ import annotations

import re

# Deliberately permissive: the goal is to catch typos and obvious rubbish, not
# to reimplement RFC 5322. Delivery of the receipt is the real proof.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]{2,}$")

MIN_NAME_LENGTH = 2
MAX_NAME_LENGTH = 80
MIN_ADDRESS_LENGTH = 6
MAX_ADDRESS_LENGTH = 250
MIN_PHONE_DIGITS = 7
MAX_PHONE_DIGITS = 15  # E.164 maximum


def clean_name(raw: str | None) -> str | None:
    name = " ".join((raw or "").split())
    if not MIN_NAME_LENGTH <= len(name) <= MAX_NAME_LENGTH:
        return None
    # A name made only of digits or punctuation is a mis-send, not a name.
    if not any(ch.isalpha() for ch in name):
        return None
    return name


def clean_email(raw: str | None) -> str | None:
    email = (raw or "").strip().strip("<>").lower()
    return email if EMAIL_RE.match(email) else None


def clean_address(raw: str | None) -> str | None:
    address = " ".join((raw or "").split())
    if not MIN_ADDRESS_LENGTH <= len(address) <= MAX_ADDRESS_LENGTH:
        return None
    return address


def clean_phone(raw: str | None, *, fallback: str | None = None) -> str | None:
    """Normalise a phone number to digits.

    'same' (or an empty reply) means "use my WhatsApp number", which is why
    `fallback` exists.
    """
    text = (raw or "").strip().lower()
    if text in {"same", "this", "yes", ""} and fallback:
        return "".join(ch for ch in fallback if ch.isdigit())

    digits = "".join(ch for ch in text if ch.isdigit())
    if not MIN_PHONE_DIGITS <= len(digits) <= MAX_PHONE_DIGITS:
        return None
    return digits


def clean_postal_code(raw: str | None) -> str | None:
    """Accept a ZIP/PIN code: 3-10 alphanumerics, spaces and dashes allowed."""
    code = (raw or "").strip().upper()
    compact = code.replace(" ", "").replace("-", "")
    if not 3 <= len(compact) <= 10 or not compact.isalnum():
        return None
    return code


def is_skip(raw: str | None, skip_tokens: set[str]) -> bool:
    return (raw or "").strip().lower() in skip_tokens
