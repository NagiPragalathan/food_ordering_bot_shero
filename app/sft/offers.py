"""Course offers (the Offers page): what a lead pays.

    Code offer       the lead sends its code on WhatsApp ("SFT50", or "SFT
                     SFT50" from an ad); it is theirs from then on.
    Automatic offer  no code: it covers everyone (or the sources ticked on it)
                     while it runs: a launch price, a weekend deal.

Each is a percent off, an amount off, or a fixed price; it runs between its
dates (SFT's timezone) and may stop after a number of enrollments. A lead
pays the lowest price of the offers that apply to them, never less than
Stripe's minimum and never more than the course fee. The price shows in the
welcome, the brochure, the FAQ, the Pay button and the Stripe checkout.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SftLead, SftOffer
from app.sft.config import SftConfig

KINDS = {"percent": "% off", "amount": "off", "price": "fixed price"}
# Stripe charges at least $0.50.
MINIMUM = Decimal("0.50")
CODE = re.compile(r"^[A-Z0-9][A-Z0-9-]{2,19}$")


@dataclass(frozen=True)
class Price:
    amount: Decimal
    fee: Decimal                   # the course fee before any offer
    offer: SftOffer | None = None

    @property
    def discounted(self) -> bool:
        return self.offer is not None and self.amount < self.fee

    @property
    def saving(self) -> Decimal:
        return self.fee - self.amount


def today(cfg: SftConfig, at: datetime | None = None) -> date:
    return (at or datetime.now(ZoneInfo(cfg.timezone))).astimezone(ZoneInfo(cfg.timezone)).date()


def price_after(fee: Decimal, offer: SftOffer) -> Decimal:
    value = Decimal(offer.value or 0)
    if offer.kind == "percent":
        amount = fee * (Decimal(100) - min(value, Decimal(100))) / Decimal(100)
    elif offer.kind == "amount":
        amount = fee - value
    else:
        amount = value
    amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return min(fee, max(MINIMUM, amount))


def describe(offer: SftOffer, cfg: SftConfig) -> str:
    """"20% off", "$20 off", "$79 fixed price"."""
    value = Decimal(offer.value or 0)
    if offer.kind == "percent":
        return f"{value.normalize():f}% off"
    if offer.kind == "amount":
        return f"{cfg.money(value)} off"
    return f"{cfg.money(value)} price"


def runs_on(offer: SftOffer, day: date) -> bool:
    return bool(offer.active) and (offer.starts_on is None or offer.starts_on <= day) and \
        (offer.ends_on is None or day <= offer.ends_on)


async def uses(session: AsyncSession, ids=None) -> dict[uuid.UUID, int]:
    """Enrollments each offer gave (real payments)."""
    query = select(SftLead.offer_id, func.count()).where(
        SftLead.offer_id.is_not(None), SftLead.paid_at.is_not(None),
        SftLead.test_payment.is_(False)).group_by(SftLead.offer_id)
    if ids is not None:
        query = query.where(SftLead.offer_id.in_(list(ids)))
    return dict((await session.execute(query)).all())


def used_up(offer: SftOffer, used: dict) -> bool:
    return offer.usage_limit is not None and used.get(offer.id, 0) >= offer.usage_limit


def status(offer: SftOffer, cfg: SftConfig, used: dict) -> tuple[str, str]:
    """(label, badge tone) for the Offers page."""
    day = today(cfg)
    if not offer.active:
        return "Off", "default"
    if offer.ends_on and day > offer.ends_on:
        return "Ended", "default"
    if used_up(offer, used):
        return "Used up", "warn"
    if offer.starts_on and day < offer.starts_on:
        return "Scheduled", "info"
    return "Running", "good"


def covers(offer: SftOffer, lead: SftLead) -> bool:
    return not offer.sources or lead.source in offer.sources


async def running(session: AsyncSession, cfg: SftConfig) -> list[SftOffer]:
    day = today(cfg)
    rows = (await session.execute(select(SftOffer).where(SftOffer.active.is_(True)))).scalars()
    return [o for o in rows if runs_on(o, day)]


async def price(session: AsyncSession, lead: SftLead | None, cfg: SftConfig) -> Price:
    """What this lead pays now: the lowest of the offers that apply."""
    fee = Decimal(cfg.course_fee)
    best = Price(fee, fee)
    if lead is None or lead.paid_at is not None:
        return best
    offers = await running(session, cfg)
    if not offers:
        return best
    used = await uses(session, [o.id for o in offers])
    for offer in offers:
        if used_up(offer, used) or not covers(offer, lead):
            continue
        if offer.code and offer.id != lead.offer_id:
            continue                      # a code offer is theirs only once they sent it
        amount = price_after(fee, offer)
        if amount < best.amount:
            best = Price(amount, fee, offer)
    return best


def fee_text(cfg: SftConfig, p: Price) -> str:
    """The fee as the messages say it: "$79 (was $99)", or "$99"."""
    if p.discounted:
        return f"{cfg.money(p.amount)} (was {cfg.money(p.fee)})"
    return cfg.money(p.amount)


async def live_codes(session: AsyncSession, cfg: SftConfig) -> list[str]:
    return [o.code for o in await running(session, cfg) if o.code]


def find_code(text: str | None, codes) -> str | None:
    """A code sent as a whole word ("sft50", "SFT SFT50", "code: SFT50 please")."""
    words = set(re.findall(r"[A-Z0-9][A-Z0-9-]*", (text or "").upper()))
    for code in codes:
        if code and code.upper() in words:
            return code.upper()
    return None


async def from_message(session: AsyncSession, lead: SftLead, text: str | None,
                       cfg: SftConfig) -> SftOffer | None:
    """The offer whose code this message carries, if it is running and the lead
    can have it (not yet paid, a source it covers, not used up)."""
    if lead.paid_at is not None or not text:
        return None
    offers = [o for o in await running(session, cfg) if o.code]
    code = find_code(text, [o.code for o in offers])
    if code is None:
        return None
    offer = next(o for o in offers if o.code.upper() == code)
    if not covers(offer, lead) or used_up(offer, await uses(session, [offer.id])):
        return None
    return offer


def clean_code(raw: str | None) -> str | None:
    code = re.sub(r"\s+", "", (raw or "").strip().upper())
    return code or None
