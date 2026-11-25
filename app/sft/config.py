"""SFT's settings, as one validated setting (SFT_CONFIG) saved from its admin page.

Off until switched on. Then either Test (only the listed numbers get the
course journey, everyone else is untouched) or Live.

Which WhatsApp number: SFT's own Gallabox channel when one is set (every
message on that number is SFT's), otherwise Shero US's number, where a chat
becomes SFT's only when it asks for it - the keyword (SFT), an SFT button, a
Click-to-WhatsApp ad listed here, or a lead already part-way through the
journey. Every other message on that number goes to the ordering bot exactly
as before.

Kept in Shero US's database; read on every WhatsApp message, so it is cached
for CACHE_SECONDS (a save clears the cache at once).
"""

from __future__ import annotations

import json
import re
import time
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import AppSetting

KEY = "SFT_CONFIG"
CACHE_SECONDS = 30

OFF, TEST, LIVE = "off", "test", "live"
MODES = {OFF: "Off: SFT answers nobody", TEST: "Test: only the test numbers below",
         LIVE: "Live: everyone"}

# Typed after the keyword ("SFT IG", "sft-web") or put in a wa.me link's text,
# these tell the analytics and Zoho where the lead came from.
DEFAULT_SOURCE_CODES: dict[str, str] = {
    "ig": "Instagram", "fb": "Facebook", "web": "Website", "yt": "YouTube",
    "qr": "QR code", "email": "Email", "ref": "Referral", "event": "Event",
}


class SftConfigError(ValueError):
    pass


# The automated nudges, each of which can be switched off on the Nudges page.
NUDGES: dict[str, str] = {
    "followup_1": "Follow-up 1", "followup_2": "Follow-up 2", "followup_3": "Follow-up 3",
    "followup_4": "Follow-up 4", "followup_5": "Follow-up 5",
    "payment_reminder": "Payment link again", "nurture": "Maybe later check-in",
    "course_check": "Course check after paying",
}


class SftConfig(BaseModel):
    mode: Literal["off", "test", "live"] = OFF
    test_numbers: list[str] = Field(default_factory=list)
    # Test mode only: the test numbers pay with a test payment (no card, no
    # charge). Off: they get the real Stripe checkout, as customers do. Live
    # mode always takes real payments.
    test_payments: bool = True
    # SFT's own Gallabox channel (WhatsApp number). Empty: Shero US's number.
    channel_id: str = ""
    # A message containing one of these (whole words) starts the journey.
    keywords: list[str] = Field(default_factory=lambda: ["sft", "shero food training",
                                                         "food training"])
    source_codes: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_SOURCE_CODES))
    # Click-to-WhatsApp ads that are SFT's: their chats start the journey.
    ad_ids: list[str] = Field(default_factory=list)

    ask_name: bool = True
    course_fee: Decimal = Field(default=Decimal("99"), gt=0, le=10000)
    currency: str = "usd"
    # A link to the brochure somewhere else. Empty: the PDF uploaded on the
    # Settings page (brochure_file, under /media/sft/), else the one this
    # server ships (/static/sft/...).
    brochure_url: str = ""
    brochure_file: str = ""
    brochure_label: str = ""      # the uploaded file's own name, for the Settings page
    # The course platform's link, sent once they have paid.
    course_url: str = ""
    course_access_note: str = ""

    # Automated follow-ups when a lead goes quiet: hours after their last
    # move (24 hours; 48-72 hours; 5-7 days).
    followup_hours: list[int] = Field(default_factory=lambda: [24, 60, 144])
    # "Maybe later": after the check-in on the day they chose, check in
    # again after these many days (each counted from the one before).
    nurture_days: list[int] = Field(default_factory=lambda: [30])
    # Payment link sent, not paid: a reminder after this many minutes.
    payment_reminder_minutes: int = Field(default=60, ge=10, le=24 * 60)
    # After paying: "have you been able to open the course?" after this long.
    access_check_hours: int = Field(default=24, ge=1, le=24 * 14)

    # Hand a lead to the team when they stop at a stage this long (hours) -
    # the drop-off table. 0 = never for that stage.
    attention_hours: dict[str, float] = Field(default_factory=lambda: {
        "new": 2, "contacted": 2, "interested": 4, "brochure_sent": 24, "questions": 12,
        "payment_pending": 12, "access": 24, "followups_done": 24, "nurture": 0,
    })

    # Follow-ups go out only between these hours, in this timezone.
    timezone: str = "America/New_York"
    send_from_hour: int = Field(default=9, ge=0, le=23)
    send_until_hour: int = Field(default=20, ge=1, le=24)

    # The automated WhatsApp nudges (Nudges page): all of them, and each one
    # (NUDGES keys) switched off on its own. The team's tasks are not nudges:
    # they are raised either way (0 hours turns one off).
    nudges_on: bool = True
    nudges_off: list[str] = Field(default_factory=list)

    # The bot's own wording, edited on the Bot replies page: {key: text}.
    # A key that is not here says app/sft/content.py's default.
    texts: dict[str, str] = Field(default_factory=dict)

    # WhatsApp numbers told when a lead asks for a person.
    team_numbers: list[str] = Field(default_factory=list)

    # Zoho CRM (Shero US's): leads, tags, notes and team tasks.
    zoho_sync: bool = True
    zoho_tasks: bool = True
    # Set by the sync when Zoho says the plan has no tags (Free edition):
    # the stage then lives in Lead Status and the notes. Prepare Zoho clears it.
    zoho_no_tags: bool = False

    # The Shero Training course platform (app/sft/platform.py): once a lead
    # pays, it makes their account and course invite, and the bot sends them
    # the login. Its secret is SFT_PLATFORM_WEBHOOK_SECRET (settings_store).
    platform_api_url: str = "https://api.sherosft.com/api"
    platform_login_url: str = "https://sft.sherosft.com/partner-login"
    # Test payments make real accounts (and invite emails) there only when on.
    platform_for_tests: bool = False

    @field_validator("test_numbers", "team_numbers", mode="before")
    @classmethod
    def _numbers(cls, value):
        items = value.split(",") if isinstance(value, str) else (value or [])
        found = []
        for item in items:
            digits = "".join(ch for ch in str(item).split("|")[0] if ch.isdigit())
            if not digits:
                continue
            if not 10 <= len(digits) <= 15:
                raise ValueError(f"{item!r} is not a WhatsApp number with its country code")
            if digits not in found:
                found.append(digits)
        return found

    @field_validator("platform_api_url", "platform_login_url", mode="before")
    @classmethod
    def _platform_url(cls, value):
        url = str(value or "").strip().rstrip("/")
        if not re.fullmatch(r"https://[^\s/]+(/\S*)?", url):
            raise ValueError("the course platform's addresses start with https://")
        return url

    @field_validator("keywords", "ad_ids", mode="before")
    @classmethod
    def _words(cls, value):
        items = re.split(r"[,\n]", value) if isinstance(value, str) else (value or [])
        found = []
        for item in items:
            word = " ".join(str(item).split()).strip()
            if word and word.lower() not in [f.lower() for f in found]:
                found.append(word[:60])
        return found

    @field_validator("keywords")
    @classmethod
    def _need_keyword(cls, value):
        if not value:
            raise ValueError("keep at least one keyword, or nobody can start the journey")
        return value

    @field_validator("source_codes", mode="before")
    @classmethod
    def _codes(cls, value):
        if isinstance(value, str):
            pairs = {}
            for line in value.splitlines():
                if "=" in line:
                    code, name = line.split("=", 1)
                    if code.strip() and name.strip():
                        pairs[code.strip().lower()[:20]] = name.strip()[:40]
            return pairs
        return {str(k).strip().lower()[:20]: str(v).strip()[:40]
                for k, v in (value or {}).items() if str(k).strip() and str(v).strip()}

    @field_validator("followup_hours", "nurture_days", mode="before")
    @classmethod
    def _steps(cls, value):
        items = re.split(r"[,\s]+", value) if isinstance(value, str) else (value or [])
        numbers = [int(float(x)) for x in items if str(x).strip()]
        if any(n <= 0 for n in numbers):
            raise ValueError("each wait must be more than 0")
        if numbers != sorted(numbers) or len(set(numbers)) != len(numbers):
            raise ValueError("list the waits from shortest to longest")
        if len(numbers) > 5:
            raise ValueError("at most 5")
        return numbers

    @field_validator("nudges_off", mode="before")
    @classmethod
    def _nudges(cls, value):
        return [k for k in dict.fromkeys(value or []) if k in NUDGES]

    @field_validator("texts", mode="before")
    @classmethod
    def _texts(cls, value):
        return {str(k): str(v).strip() for k, v in (value or {}).items() if str(v or "").strip()}

    @field_validator("currency")
    @classmethod
    def _currency(cls, value):
        value = (value or "usd").strip().lower()
        if not re.fullmatch(r"[a-z]{3}", value):
            raise ValueError("a 3-letter currency code, such as usd")
        return value

    @field_validator("brochure_url", "course_url")
    @classmethod
    def _url(cls, value):
        value = (value or "").strip()
        if value and not value.startswith("https://"):
            raise ValueError("a link starting with https://")
        return value

    @field_validator("brochure_file")
    @classmethod
    def _brochure_file(cls, value):
        value = (value or "").strip()
        if value and not re.fullmatch(r"[0-9a-f]{8,64}/[A-Za-z0-9._-]{1,120}\.pdf", value):
            raise ValueError("an uploaded brochure's name")
        return value

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value):
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(value)
        except Exception:  # noqa: BLE001 - any unknown name
            raise ValueError(f"{value!r} is not a timezone, such as America/New_York") from None
        return value

    # --- reading it -------------------------------------------------------------
    @property
    def is_on(self) -> bool:
        return self.mode != OFF

    def answers(self, number: str) -> bool:
        """Does SFT run the journey for this number (test mode: listed ones only)?"""
        if self.mode == LIVE:
            return True
        if self.mode == TEST:
            digits = "".join(ch for ch in number if ch.isdigit())
            return any(digits == n or digits.endswith(n) for n in self.test_numbers)
        return False

    def fee_label(self) -> str:
        return self.money(self.course_fee)

    def money(self, amount) -> str:
        text = f"{Decimal(amount or 0):,.2f}".removesuffix(".00")
        return f"${text}" if self.currency == "usd" else f"{text} {self.currency.upper()}"

    def nudge_on(self, key: str) -> bool:
        """Is this automated nudge (NUDGES) sending?"""
        return self.nudges_on and key not in self.nudges_off

    def brochure_link(self) -> str:
        if self.brochure_url:
            return self.brochure_url
        from app.sft import brochure

        base = settings.public_base_url.rstrip("/")
        if self.brochure_file and brochure.path_of(self.brochure_file) is not None:
            return f"{base}/media/sft/{self.brochure_file}"
        return f"{base}/static/sft/{BROCHURE_FILE}"


BROCHURE_FILE = "shero-food-training-brochure.pdf"

_cache: dict[str, tuple[float, SftConfig]] = {}


def parse(raw: str | None) -> SftConfig:
    """The stored JSON as a config; defaults when nothing (or nothing readable) is saved."""
    try:
        data = json.loads(raw) if (raw or "").strip() else {}
        return SftConfig.model_validate(data if isinstance(data, dict) else {})
    except (ValueError, ValidationError):
        return SftConfig()


async def load(session: AsyncSession, *, fresh: bool = False) -> SftConfig:
    cached = _cache.get("cfg")
    if cached and not fresh and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    row = (await session.execute(select(AppSetting).where(AppSetting.key == KEY))).scalar_one_or_none()
    cfg = parse(row.value if row else None)
    _cache["cfg"] = (time.monotonic(), cfg)
    return cfg


async def save(session: AsyncSession, data: dict, actor: str) -> SftConfig:
    try:
        cfg = SftConfig.model_validate(data)
    except ValidationError as exc:
        raise SftConfigError(readable(exc)) from None
    row = (await session.execute(select(AppSetting).where(AppSetting.key == KEY))).scalar_one_or_none()
    if row is None:
        row = AppSetting(key=KEY, is_secret=False)
        session.add(row)
    row.value = cfg.model_dump_json()
    row.updated_by = actor
    await session.flush()
    clear_cache()
    return cfg


def clear_cache() -> None:
    _cache.clear()


def readable(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        where = ".".join(str(p) for p in error.get("loc", ()) if p != "__root__")
        message = str(error.get("msg", "")).removeprefix("Value error, ")
        parts.append(f"{where.replace('_', ' ')}: {message}" if where else message)
    return "; ".join(parts)
