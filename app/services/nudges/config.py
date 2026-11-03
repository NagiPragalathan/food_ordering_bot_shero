"""What the nudges do, as one validated setting (NUDGE_RULES).

Targeting rules: for each stage of the ordering funnel, wait so many hours in
the stage, send a message (once per time they enter it), and if they are
still in the stage so many hours later, move them to No response.

Retargeting sequences: tapping No or Later on a message, or not answering,
puts the customer in that bucket. Each bucket sends a first message after a
wait, then follow-ups in phases ("every 1 day x 2, then every 10 days x 3,
then every 30 days"), until they are a Dead Lead.

Messages are the WhatsApp templates the rules send. Each carries the buttons
Order Food, Later and No (services/nudges/messages.py).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AppSetting, LeadStage, NudgeBucket

KEY = "NUDGE_RULES"

# Every stage but Converted: a customer who has paid is not nudged.
STAGES: tuple[str, ...] = tuple(s.value for s in LeadStage if s != LeadStage.CONVERTED)
# The order the buckets show in, with their names on the page.
BUCKETS: dict[str, str] = {NudgeBucket.NO_RESPONSE: "No response", NudgeBucket.LATER: "Later",
                           NudgeBucket.NO: "No"}
# Food goes cold faster than an enquiry: a cart or a payment is chased sooner.
DEFAULT_WAIT_HOURS: dict[str, int] = {
    s.value: 3 for s in (LeadStage.CART_CREATED, LeadStage.OUTLET_SELECTED,
                         LeadStage.SLOT_SELECTED, LeadStage.PAYMENT_ABANDONED,
                         LeadStage.PAYMENT_FAILED)}
MAX_HOURS = 24 * 90
NAME_RE = re.compile(r"^[a-z0-9_]{3,100}$")
# The buttons on a message written on the Nudges page (services/nudges/replies.py).
BUTTONS = ("Order Food", "Later", "No")


class NudgeConfigError(ValueError):
    pass


class Message(BaseModel):
    """One WhatsApp template. Its text may hold {name}, the customer's first name.

    Written on the page: it has the three BUTTONS. Added from WhatsApp (an
    approved template already there, such as the 54 SMO messages): it keeps
    the quick replies it was approved with - often none, the text asking the
    customer to send ORDER FOOD."""

    name: str
    text: str = Field(min_length=10, max_length=1024)
    label: str | None = Field(default=None, max_length=80)
    buttons: list[str] | None = None          # None: BUTTONS
    source: Literal["page", "whatsapp"] = "page"
    created_at: datetime | None = None
    created_by: str | None = None

    @property
    def button_labels(self) -> tuple[str, ...]:
        return BUTTONS if self.buttons is None else tuple(self.buttons)

    @property
    def title(self) -> str:
        """What the team calls it: its label, or the start of its text."""
        if self.label:
            return self.label
        first = next((line.strip() for line in self.text.splitlines() if line.strip()), self.name)
        return first if len(first) <= 60 else first[:57].rstrip() + "..."

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not NAME_RE.match(value):
            raise ValueError("a message name is 3-100 lowercase letters, digits or _")
        return value

    @field_validator("text")
    @classmethod
    def _text(cls, value: str) -> str:
        value = value.strip()
        if re.search(r"\{(?!name\})[^}]*\}", value) or "{{" in value:
            raise ValueError("the only placeholder a message may use is {name}")
        if value.startswith("{name}") or value.rstrip(" .!?,:;-").endswith("{name}"):
            raise ValueError("WhatsApp does not allow a message to start or end with {name}")
        return value


class StageRule(BaseModel):
    stage: str
    active: bool = False
    wait_hours: int = Field(default=24, ge=1, le=MAX_HOURS)
    message: str | None = None
    # None: they stay in the stage, never moved to No response.
    no_response_hours: int | None = Field(default=24, ge=1, le=MAX_HOURS)

    @field_validator("stage")
    @classmethod
    def _stage(cls, value: str) -> str:
        if value not in STAGES:
            raise ValueError(f"unknown stage {value!r}")
        return value

    @model_validator(mode="after")
    def _needs_message(self) -> StageRule:
        if self.active and not self.message:
            raise ValueError(f"{self.stage}: choose a message before turning the rule on")
        return self


class Phase(BaseModel):
    every_days: int = Field(ge=1, le=365)
    times: int | None = Field(default=None, ge=1, le=50)       # None: repeats until Dead Lead


class Sequence(BaseModel):
    active: bool = False
    first_after: int = Field(default=1, ge=1, le=MAX_HOURS)
    first_unit: Literal["hours", "days"] = "days"
    first_message: str | None = None
    follow_up_message: str | None = None     # None: the first message again
    phases: list[Phase] = Field(default_factory=list, max_length=3)
    dead_after_days: int | None = Field(default=180, ge=1, le=3650)
    dead_after_messages: int | None = Field(default=None, ge=1, le=100)
    # Sent the moment they tap the button that put them here (No, Later).
    reply: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _shape(self) -> Sequence:
        if self.active and not self.first_message:
            raise ValueError("choose a first message before turning the sequence on")
        if any(p.times is None for p in self.phases[:-1]):
            raise ValueError("only the last phase may repeat; give the others a number of times")
        return self

    def delay(self, sent: int) -> timedelta | None:
        """How long after the last message (or entering the bucket, when
        nothing has gone yet) the next one is due. None: no more messages."""
        if sent == 0:
            return timedelta(**{self.first_unit: self.first_after})
        left = sent - 1
        for phase in self.phases:
            if phase.times is None or left < phase.times:
                return timedelta(days=phase.every_days)
            left -= phase.times
        return None

    def message_for(self, sent: int) -> str | None:
        return self.first_message if sent == 0 else (self.follow_up_message or self.first_message)

    def timeline(self, limit: int = 8) -> list[str]:
        """When each message goes, for the page: "24 hours in", "+1 day"..."""
        steps: list[str] = []
        while len(steps) < limit:
            if self.dead_after_messages and len(steps) >= self.dead_after_messages:
                break
            delay = self.delay(len(steps))
            if delay is None:
                break
            if not steps:
                n, unit = self.first_after, self.first_unit
                steps.append(f"{n} {unit[:-1] if n == 1 else unit} in")
            else:
                steps.append(f"+{delay.days} day{'' if delay.days == 1 else 's'}")
        return steps

    @property
    def repeats(self) -> bool:
        return bool(self.phases) and self.phases[-1].times is None and not self.dead_after_messages


def _default_rules() -> list[StageRule]:
    return [StageRule(stage=s, wait_hours=DEFAULT_WAIT_HOURS.get(s, 24)) for s in STAGES]


def _default_sequences() -> dict[str, Sequence]:
    """KOB CRM's cadence, switched off until messages are chosen."""
    return {
        NudgeBucket.NO_RESPONSE: Sequence(
            first_after=24, first_unit="hours",
            phases=[Phase(every_days=1, times=2), Phase(every_days=10, times=3),
                    Phase(every_days=30)]),
        NudgeBucket.LATER: Sequence(
            first_after=7, first_unit="days",
            phases=[Phase(every_days=10, times=2), Phase(every_days=30)],
            reply="No problem! We'll check back with you in a few days. "
                  "Tap Order Food whenever you're ready."),
        NudgeBucket.NO: Sequence(
            first_after=30, first_unit="days", dead_after_messages=1,
            reply="Thanks for letting us know. We won't message you about this for a while."),
    }


class NudgeSettings(BaseModel):
    enabled: bool = False
    # Messages only go between these hours, in this time zone.
    send_from: int = Field(default=9, ge=0, le=23)
    send_to: int = Field(default=21, ge=1, le=24)
    timezone: str = "America/New_York"
    zoho_sync: bool = True
    rules: list[StageRule] = Field(default_factory=_default_rules)
    sequences: dict[str, Sequence] = Field(default_factory=_default_sequences)
    messages: list[Message] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except Exception:  # noqa: BLE001 - any bad name
            raise ValueError(f"unknown time zone {value!r}") from None
        return value

    @model_validator(mode="after")
    def _complete(self) -> NudgeSettings:
        if self.send_from >= self.send_to:
            raise ValueError("the send hours must start before they end")
        # One rule per stage, in funnel order; a stage added later gets a default.
        given = {r.stage: r for r in self.rules}
        self.rules = [given.get(s) or StageRule(stage=s, wait_hours=DEFAULT_WAIT_HOURS.get(s, 24))
                      for s in STAGES]
        defaults = _default_sequences()
        self.sequences = {b: self.sequences.get(b) or defaults[b] for b in BUCKETS}
        names = [m.name for m in self.messages]
        if len(names) != len(set(names)):
            raise ValueError("two messages have the same name")
        used = [r.message for r in self.rules] + [
            n for s in self.sequences.values() for n in (s.first_message, s.follow_up_message)]
        missing = sorted({n for n in used if n and n not in names})
        if missing:
            raise ValueError(f"no such message: {', '.join(missing)}")
        return self

    def rule(self, stage: str) -> StageRule | None:
        return next((r for r in self.rules if r.stage == stage), None)

    def message(self, name: str | None) -> Message | None:
        return next((m for m in self.messages if m.name == name), None)

    def uses(self, name: str) -> list[str]:
        """Where a message is used, for the page."""
        where = [f"{r.stage}" for r in self.rules if r.message == name]
        for bucket, seq in self.sequences.items():
            if name in (seq.first_message, seq.follow_up_message):
                where.append(BUCKETS[bucket])
        return where


def readable(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(str(e.get("msg", "")).removeprefix("Value error, ") for e in exc.errors())
    return str(exc)


async def load(session: AsyncSession) -> NudgeSettings:
    row = (await session.execute(select(AppSetting).where(AppSetting.key == KEY))).scalar_one_or_none()
    return NudgeSettings.model_validate_json(row.value) if row and row.value else NudgeSettings()


async def save(session: AsyncSession, data: dict, actor: str) -> NudgeSettings:
    try:
        saved = NudgeSettings.model_validate(data)
    except ValueError as exc:
        raise NudgeConfigError(readable(exc)) from None
    row = (await session.execute(select(AppSetting).where(AppSetting.key == KEY))).scalar_one_or_none()
    if row is None:
        row = AppSetting(key=KEY, is_secret=False)
        session.add(row)
    row.value, row.updated_by = saved.model_dump_json(), actor
    await session.flush()
    return saved
