"""Money, distances, postal codes and phone numbers in the current
business's own terms: dollars, miles and ZIP codes for Shero US; rupees,
kilometres and PIN codes for DST India (app/core/tenants.py).

Every place that shows one of these to a customer or the team goes through
here, so the same page or message reads right for either business. Also
available in every template as `locale` (app/templating.py).
"""

from __future__ import annotations

import re
from decimal import Decimal

from app.core.config import settings

KM_PER_MILE = 1.609344


# --- money ---------------------------------------------------------------------
def currency() -> str:
    """ISO code: USD, INR."""
    return settings.currency.upper()


def symbol() -> str:
    return settings.currency_symbol


def money(amount) -> str:
    """"$12.50", "₹249.00"."""
    return f"{symbol()}{Decimal(str(amount or 0)):.2f}"


_MONEY_NOISE = re.compile(r"(?i)\$|₹|\brs\.?|\binr\b|\busd\b|,|\s")


def amount_text(raw: str) -> str:
    """What people type for an amount, cleaned to a number's text:
    "$16.53", "₹1,249", "Rs. 249", " 16 " -> "16.53", "1249", "249", "16"."""
    return _MONEY_NOISE.sub("", raw or "")


def plain_money(amount) -> str:
    """Without trailing zeros: "$5", "$4.50", "₹249"."""
    value = Decimal(str(amount or 0)).quantize(Decimal("0.01"))
    text = f"{value:.2f}"
    if text.endswith(".00"):
        text = text[:-3]
    return f"{symbol()}{text}"


# --- distance ------------------------------------------------------------------
def distance_unit() -> str:
    """"mi" or "km": what this business reads distances in."""
    return settings.distance_unit


def unit_name(value: float | None = None) -> str:
    """"miles"/"mile" or "km"."""
    if distance_unit() == "km":
        return "km"
    return "mile" if value is not None and float(value) == 1 else "miles"


def from_km(km: float | None) -> float | None:
    """A stored distance (always km) in this business's unit, 2 places."""
    if km is None:
        return None
    value = float(km) / KM_PER_MILE if distance_unit() == "mi" else float(km)
    return round(value, 2)


def to_km(value: float) -> float:
    """A distance typed in this business's unit, as stored (km)."""
    return round(float(value) * KM_PER_MILE, 3) if distance_unit() == "mi" else float(value)


def distance(km: float | None) -> str:
    """"3.2 miles", "5 km"."""
    shown = from_km(km)
    if shown is None:
        return ""
    return f"{shown:g} {unit_name(shown)}"


# --- postal codes ----------------------------------------------------------------
def postal_label() -> str:
    """"ZIP code" / "PIN code"."""
    return settings.postal_code_label


def postal_short() -> str:
    """"ZIP" / "PIN"."""
    return postal_label().split()[0]


def postal_digits() -> int:
    return settings.postal_code_digits


def trailing_postal_code() -> re.Pattern:
    """A postal code at the very end of a typed address: a US ZIP (or ZIP+4),
    an Indian 6-digit PIN."""
    digits = postal_digits()
    plus4 = r"(?:-\d{4})?" if settings.country_code.upper() == "US" else ""
    return re.compile(rf"\b(\d{{{digits}}}){plus4}\s*$")


# --- phone numbers ---------------------------------------------------------------
def with_country_code(raw: str) -> str:
    """Digits only, with the business's country code put in front of a bare
    10-digit national number (7401268091 -> 917401268091 in India)."""
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    code = settings.phone_country_code.strip()
    if len(digits) == 10 and code and not digits.startswith(code + "0"):
        return code + digits
    return digits


def phone_display(raw: str) -> str:
    """A stored phone number as people write it: "+91 93600 56005",
    "+1 (732) 555-0199"; anything else as "+" and its digits. Empty when
    there is no number."""
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if not digits:
        return ""
    if len(digits) == 12 and digits.startswith("91"):
        return f"+91 {digits[2:7]} {digits[7:]}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+1 ({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    return "+" + digits


def typed_phone(raw: str) -> str:
    """A phone number a person typed, ready to store.

    DST India's customers and kitchens type their 10-digit mobile number
    (98400 12345): it is kept as digits with +91 in front, so WhatsApp and the
    riders can use it. Shero US has always stored a typed number exactly as
    typed, and keeps doing so."""
    if settings.country_code.upper() == "US":
        return raw
    return with_country_code(raw)
