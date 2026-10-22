"""Everything about nudges that can be customised, and its validation.

Two shapes, both plain JSON on the way in and out:

  * GlobalSettings - one per shop: dry run, test numbers, quiet hours,
    frequency caps, STOP keywords, Zoho sync. (The master switch is its own
    column, `nudge_settings.enabled`.)
  * CampaignConfig - one per campaign, in sections: trigger, audience,
    message, schedule, limits, offer, zoho.

Unknown options are refused rather than ignored (`extra="forbid"`), so a
misspelt "after_minuts" is an error message, not a setting that silently
never applies. Missing options take the defaults for the campaign's kind
(KIND_DEFAULTS), and the reference endpoint lists all of them.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timezone
from decimal import Decimal
from string import Formatter
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.db.models import LeadStage, NudgeKind

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class NudgeConfigError(ValueError):
    """A setting or campaign that cannot be saved; the message says why."""


class Strict(BaseModel):
    # A misspelt option is an error, not a setting that never applies.
    model_config = ConfigDict(extra="forbid")


def _digits(raw: str) -> str:
    return "".join(ch for ch in str(raw) if ch.isdigit())


def _numbers(values: list[str]) -> list[str]:
    cleaned = []
    for raw in values:
        digits = _digits(raw)
        if not 8 <= len(digits) <= 15:
            raise ValueError(f"{raw!r} is not a WhatsApp number (8 to 15 digits, "
                             "with the country code)")
        cleaned.append(digits)
    return cleaned


def _words(values: list[str]) -> list[str]:
    return [" ".join(str(v).lower().split()) for v in values if str(v).strip()]


# --- shared pieces -------------------------------------------------------------
class SendWindow(Strict):
    """Local hours a nudge may go out. An end before the start runs overnight
    (22:00 to 02:00)."""

    start: str = "09:00"
    end: str = "21:00"

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, value: str) -> str:
        if not HHMM.match(value or ""):
            raise ValueError("use 24-hour HH:MM, for example 09:30")
        return value

    @model_validator(mode="after")
    def _not_empty(self):
        if self.start == self.end:
            raise ValueError("start and end are the same, which leaves no time to send")
        return self

    def allows(self, moment: time) -> bool:
        start, end = time.fromisoformat(self.start), time.fromisoformat(self.end)
        if start < end:
            return start <= moment < end
        return moment >= start or moment < end


# --- global settings -------------------------------------------------------------
DEFAULT_STOP = ["stop", "stop promotions", "stop offers", "unsubscribe", "opt out"]
DEFAULT_START = ["start offers", "start promotions", "subscribe", "opt in"]


class GlobalSettings(Strict):
    # Log who would be nudged, send nothing. For watching a new campaign for a
    # day before it goes live.
    dry_run: bool = False
    # When set, nudges go to these numbers only: a live test on real data.
    test_numbers: list[str] = Field(default_factory=list)
    # Quiet hours are judged in the customer's kitchen's timezone; this one
    # is for customers with no kitchen yet.
    timezone: str = "America/New_York"
    send_window: SendWindow = Field(default_factory=SendWindow)
    # Across all campaigns together.
    max_per_customer_per_day: int = Field(1, ge=1, le=10)
    max_per_customer_per_week: int = Field(3, ge=1, le=50)
    min_hours_between: float = Field(20, ge=0, le=720)
    max_sends_per_run: int = Field(50, ge=1, le=1000)
    # A paid order this many days after a nudge counts as its conversion.
    attribution_days: int = Field(3, ge=1, le=60)
    # Meta does not deliver MARKETING templates to US (+1) numbers. Sending
    # one costs a message and reaches nobody, so it is skipped instead.
    block_marketing_templates_to_us: bool = True
    stop_keywords: list[str] = Field(default_factory=lambda: list(DEFAULT_STOP))
    start_keywords: list[str] = Field(default_factory=lambda: list(DEFAULT_START))
    opt_out_reply: str = Field(
        "Done - you won't get offers or reminders from us any more. Your order "
        "updates still come through. Reply START OFFERS to turn them back on.",
        min_length=1, max_length=1000)
    opt_in_reply: str = Field(
        "You're back on our list - we'll send you offers and reminders again. "
        "Reply STOP any time to stop them.", min_length=1, max_length=1000)
    # Record each nudge on the customer's Lead/Contact in Zoho.
    zoho_sync: bool = True
    zoho_notes: bool = True
    zoho_opt_out_tag: str | None = Field("WhatsApp Opt-out", max_length=50)
    shop_name: str = Field("Shero Home Food", min_length=1, max_length=60)

    @field_validator("test_numbers")
    @classmethod
    def _test_numbers(cls, values: list[str]) -> list[str]:
        return _numbers(values)

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"{value!r} is not a timezone, e.g. America/New_York") from None
        return value

    @field_validator("stop_keywords", "start_keywords")
    @classmethod
    def _keywords(cls, values: list[str]) -> list[str]:
        cleaned = _words(values)
        if not cleaned:
            raise ValueError("needs at least one keyword")
        return cleaned

    @model_validator(mode="after")
    def _keywords_apart(self):
        clash = set(self.stop_keywords) & set(self.start_keywords)
        if clash:
            raise ValueError(f"{sorted(clash)} cannot both stop and start offers")
        if self.max_per_customer_per_day > self.max_per_customer_per_week:
            raise ValueError("max_per_customer_per_day is more than max_per_customer_per_week")
        return self


# --- a campaign --------------------------------------------------------------------
class Trigger(Strict):
    # abandoned_cart / abandoned_checkout: how long it has been left, and the
    # oldest it may be (a two-week-old cart is not worth a reminder).
    after_minutes: int | None = Field(None, ge=5, le=60 * 24 * 30)
    within_hours: int | None = Field(None, ge=1, le=24 * 90)
    min_value: Decimal | None = Field(None, ge=0)
    min_items: int | None = Field(None, ge=1, le=100)
    payment_statuses: list[Literal["expired", "failed"]] | None = None
    # abandoned_checkout: put the unpaid order's dishes back in an empty cart,
    # so the ordering page opens with them ready (prices are re-read at checkout).
    restore_cart: bool | None = None
    # repeat_purchase: days since the last paid order, and optionally a limit
    # (14 to 30 days = a reminder; 60+ = a win-back, as its own campaign).
    days_since_last_order: int | None = Field(None, ge=1, le=3650)
    max_days_since_last_order: int | None = Field(None, ge=1, le=3650)
    # never_ordered: days since they first wrote in.
    days_since_signup: int | None = Field(None, ge=0, le=3650)
    max_days_since_signup: int | None = Field(None, ge=1, le=3650)
    require_details: bool | None = None


TRIGGER_FIELDS: dict[str, tuple[str, ...]] = {
    NudgeKind.ABANDONED_CART: ("after_minutes", "within_hours", "min_value", "min_items"),
    NudgeKind.ABANDONED_CHECKOUT: ("after_minutes", "within_hours", "min_value",
                                   "payment_statuses", "restore_cart"),
    NudgeKind.REPEAT_PURCHASE: ("days_since_last_order", "max_days_since_last_order"),
    NudgeKind.NEVER_ORDERED: ("days_since_signup", "max_days_since_signup", "require_details"),
    NudgeKind.TARGETED_OFFER: (),
}


class Audience(Strict):
    """Narrows any campaign. Every filter left out lets everyone through."""

    min_paid_orders: int | None = Field(None, ge=0)
    max_paid_orders: int | None = Field(None, ge=0)
    min_total_spent: Decimal | None = Field(None, ge=0)
    max_total_spent: Decimal | None = Field(None, ge=0)
    last_order_within_days: int | None = Field(None, ge=1)
    last_order_older_than_days: int | None = Field(None, ge=1)
    signed_up_within_days: int | None = Field(None, ge=1)
    signed_up_older_than_days: int | None = Field(None, ge=0)
    # Matched case-insensitively against the customer's chosen cuisine.
    cuisines: list[str] = Field(default_factory=list)
    # Kitchen codes: the customer's kitchen, or the one their last order came from.
    kitchens: list[str] = Field(default_factory=list)
    # Prefixes: "088" matches 08820 and 08854.
    postal_codes: list[str] = Field(default_factory=list)
    lead_stages: list[str] = Field(default_factory=list)
    lead_sources: list[str] = Field(default_factory=list)
    has_email: bool | None = None
    # Dish names or menu codes they have paid for before, any of them.
    ordered_dishes: list[str] = Field(default_factory=list)
    # Number prefixes: "1" for the US and Canada, "91" for India.
    country_codes: list[str] = Field(default_factory=list)
    exclude_country_codes: list[str] = Field(default_factory=list)
    whatsapp_numbers: list[str] = Field(default_factory=list)
    exclude_whatsapp_numbers: list[str] = Field(default_factory=list)

    @field_validator("whatsapp_numbers", "exclude_whatsapp_numbers")
    @classmethod
    def _numbers(cls, values: list[str]) -> list[str]:
        return _numbers(values)

    @field_validator("country_codes", "exclude_country_codes")
    @classmethod
    def _codes(cls, values: list[str]) -> list[str]:
        cleaned = [_digits(v) for v in values]
        if any(not 1 <= len(code) <= 4 for code in cleaned):
            raise ValueError("country codes are 1 to 4 digits, e.g. 1 or 91")
        return cleaned

    @field_validator("cuisines", "lead_sources", "ordered_dishes")
    @classmethod
    def _lower(cls, values: list[str]) -> list[str]:
        return _words(values)

    @field_validator("kitchens")
    @classmethod
    def _kitchens(cls, values: list[str]) -> list[str]:
        return [v.strip().upper() for v in values if v.strip()]

    @field_validator("postal_codes")
    @classmethod
    def _postal(cls, values: list[str]) -> list[str]:
        return [v.strip().upper() for v in values if v.strip()]

    @field_validator("lead_stages")
    @classmethod
    def _stages(cls, values: list[str]) -> list[str]:
        known = {str(s).lower(): str(s) for s in LeadStage}
        cleaned = []
        for value in values:
            stage = known.get(value.strip().lower())
            if stage is None:
                raise ValueError(f"{value!r} is not a lead stage; one of {sorted(known.values())}")
            cleaned.append(stage)
        return cleaned

    @model_validator(mode="after")
    def _ranges(self):
        for low, high in (("min_paid_orders", "max_paid_orders"),
                          ("min_total_spent", "max_total_spent"),
                          ("last_order_within_days", "last_order_older_than_days"),
                          ("signed_up_within_days", "signed_up_older_than_days")):
            a, b = getattr(self, low), getattr(self, high)
            if a is None or b is None:
                continue
            # "Within 30 days but older than 7": within must be the larger.
            ok = a <= b if low.startswith("min") else a > b
            if not ok:
                raise ValueError(f"{low} and {high} leave nobody in the audience")
        return self


class TemplateMessage(Strict):
    """An approved WhatsApp template, for customers outside the 24-hour window
    (WhatsApp allows only templates there). Leave `name` empty and those
    customers are skipped."""

    name: str | None = Field(None, max_length=120)
    # Values for {{1}}, {{2}}... in order; each may use placeholders.
    params: list[str] = Field(default_factory=list, max_length=10)
    # "order_link": the template's URL button opens the customer's ordering
    # page (its base must be https://<your domain>/order/{{1}}).
    button: Literal["none", "order_link"] = "none"
    # Use the template even inside the window.
    always: bool = False

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        value = (value or "").strip()
        if value and not re.match(r"^[a-z0-9_]+$", value):
            raise ValueError("template names are lower case letters, digits and _")
        return value or None


class Message(Strict):
    # What the customer reads, with {placeholders} (see PLACEHOLDERS).
    text: str = Field(..., min_length=1, max_length=900)
    # "order_link" adds a button to their personal ordering page.
    button: Literal["none", "order_link", "url"] = "order_link"
    button_text: str = Field("Order now", min_length=1, max_length=20)
    button_url: str | None = Field(None, max_length=500)
    template: TemplateMessage = Field(default_factory=TemplateMessage)

    @model_validator(mode="after")
    def _url(self):
        if self.button == "url" and not (self.button_url or "").startswith("https://"):
            raise ValueError("button_url must be an https:// link when button is 'url'")
        return self


class Schedule(Strict):
    # Hours for this campaign; empty uses the global send_window.
    send_window: SendWindow | None = None
    # Days it may send on; empty is every day.
    days: list[str] = Field(default_factory=list)
    # The campaign only runs between these (UTC unless an offset is given).
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    @field_validator("days")
    @classmethod
    def _days(cls, values: list[str]) -> list[str]:
        cleaned = [v.strip().lower()[:3] for v in values]
        bad = [v for v in cleaned if v not in DAYS]
        if bad:
            raise ValueError(f"days are {list(DAYS)}")
        return cleaned

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    @model_validator(mode="after")
    def _order(self):
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValueError("ends_at is before starts_at")
        return self


class Limits(Strict):
    # Sends of this campaign to one customer, ever. Empty is no limit.
    max_per_customer: int | None = Field(None, ge=1, le=1000)
    # Days before this campaign may reach the same customer again.
    cooldown_days: float = Field(3, ge=0, le=3650)
    # Sends of this campaign per day, all customers together.
    daily_cap: int | None = Field(None, ge=1)
    per_run_cap: int | None = Field(None, ge=1)


class Offer(Strict):
    # Put the code on their cart, so the ordering page opens with it applied.
    auto_apply: bool = True
    # Skip customers who cannot use the code (first order only, used up...).
    require_eligible: bool = True


class ZohoOptions(Strict):
    # Added to their Lead/Contact when the nudge goes, e.g. "Nudge: cart".
    tag: str | None = Field(None, max_length=50)
    note: bool = True


class CampaignConfig(Strict):
    trigger: Trigger = Field(default_factory=Trigger)
    audience: Audience = Field(default_factory=Audience)
    message: Message
    schedule: Schedule = Field(default_factory=Schedule)
    limits: Limits = Field(default_factory=Limits)
    offer: Offer = Field(default_factory=Offer)
    zoho: ZohoOptions = Field(default_factory=ZohoOptions)


# --- per-kind defaults -----------------------------------------------------------------
KIND_DESCRIPTIONS = {
    NudgeKind.ABANDONED_CART: "Dishes left in the cart and no order placed.",
    NudgeKind.ABANDONED_CHECKOUT: "An order was placed but never paid (link expired or "
                                  "payment failed), and nothing paid since.",
    NudgeKind.REPEAT_PURCHASE: "Ordered before, nothing for a while - a reminder, or a "
                               "win-back with a longer gap and a coupon.",
    NudgeKind.NEVER_ORDERED: "Wrote in but never paid for an order.",
    NudgeKind.TARGETED_OFFER: "Everyone the audience filters match, once - for an offer "
                              "to a segment.",
}

KIND_DEFAULTS: dict[str, dict] = {
    NudgeKind.ABANDONED_CART: {
        "trigger": {"after_minutes": 60, "within_hours": 23, "min_items": 1},
        "message": {"text": "Hi {first_name}, your cart is still waiting: {cart_items}. "
                            "Tap below to finish your order."},
        "limits": {"max_per_customer": None, "cooldown_days": 3},
        "zoho": {"tag": "Nudge: Abandoned cart"},
    },
    NudgeKind.ABANDONED_CHECKOUT: {
        "trigger": {"after_minutes": 180, "within_hours": 72,
                    "payment_statuses": ["expired", "failed"], "restore_cart": True},
        "message": {"text": "Hi {first_name}, your order {order_number} ({order_items}) "
                            "wasn't paid, so it hasn't been cooked. Want to place it again?"},
        "limits": {"max_per_customer": None, "cooldown_days": 3},
        "zoho": {"tag": "Nudge: Unpaid order"},
    },
    NudgeKind.REPEAT_PURCHASE: {
        "trigger": {"days_since_last_order": 14},
        "message": {"text": "Hi {first_name}, it's been {days_since_last_order} days since "
                            "your last {shop_name} meal. Fancy {last_order_items} again?"},
        "limits": {"max_per_customer": None, "cooldown_days": 7},
        "zoho": {"tag": "Nudge: Repeat purchase"},
    },
    NudgeKind.NEVER_ORDERED: {
        "trigger": {"days_since_signup": 1, "max_days_since_signup": 30,
                    "require_details": False},
        "message": {"text": "Hi {first_name}, home-cooked meals from {shop_name} are a tap "
                            "away. Have a look at today's menu!"},
        "limits": {"max_per_customer": 1, "cooldown_days": 0},
        "zoho": {"tag": "Nudge: First order"},
    },
    NudgeKind.TARGETED_OFFER: {
        "message": {"text": "Hi {first_name}, a little something from {shop_name}: "
                            "{coupon_offer} with code {coupon_code}. {coupon_ends}"},
        "limits": {"max_per_customer": 1, "cooldown_days": 0},
        "zoho": {"tag": "Nudge: Offer"},
    },
}


# --- placeholders -------------------------------------------------------------------------
ALL_KINDS = tuple(str(k) for k in NudgeKind)
PLACEHOLDERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "name": ("Their name, or 'there' when we have none", ALL_KINDS),
    "first_name": ("First word of their name, or 'there'", ALL_KINDS),
    "shop_name": ("The shop name from settings", ALL_KINDS),
    "order_link": ("Link to their personal ordering page (valid 6 hours)", ALL_KINDS),
    "kitchen": ("Their kitchen's name", ALL_KINDS),
    "paid_orders": ("How many orders they have paid for", ALL_KINDS),
    "total_spent": ("What they have spent in total, e.g. $84.20", ALL_KINDS),
    "cart_items": ("The cart, e.g. '2 x Sambar, 1 x Rasam'", (NudgeKind.ABANDONED_CART,)),
    "cart_total": ("The cart's dish total, e.g. $24.50", (NudgeKind.ABANDONED_CART,)),
    "cart_count": ("How many dishes are in the cart", (NudgeKind.ABANDONED_CART,)),
    "order_number": ("The unpaid order's number", (NudgeKind.ABANDONED_CHECKOUT,)),
    "order_total": ("The unpaid order's total", (NudgeKind.ABANDONED_CHECKOUT,)),
    "order_items": ("The unpaid order's dishes", (NudgeKind.ABANDONED_CHECKOUT,)),
    "last_order_items": ("Dishes in their last paid order",
                         (NudgeKind.REPEAT_PURCHASE, NudgeKind.TARGETED_OFFER)),
    "last_order_date": ("Date of their last paid order, e.g. 3 Oct",
                        (NudgeKind.REPEAT_PURCHASE, NudgeKind.TARGETED_OFFER)),
    "days_since_last_order": ("Days since their last paid order",
                              (NudgeKind.REPEAT_PURCHASE, NudgeKind.TARGETED_OFFER)),
    "coupon_code": ("The campaign's coupon code", ALL_KINDS),
    "coupon_title": ("The coupon's title", ALL_KINDS),
    "coupon_offer": ("What it gives, e.g. '20% off, up to $10'", ALL_KINDS),
    "coupon_terms": ("Its small print, e.g. 'Min order $30 · Once per customer'", ALL_KINDS),
    "coupon_ends": ("'Valid till 31 Oct.' when it has an end date, else empty", ALL_KINDS),
}
COUPON_PLACEHOLDERS = {"coupon_code", "coupon_title", "coupon_offer", "coupon_terms",
                       "coupon_ends"}


def placeholders_in(text: str) -> list[str]:
    """The {names} a text uses. Raises NudgeConfigError on a malformed one."""
    try:
        parsed = list(Formatter().parse(text or ""))
    except ValueError as exc:
        raise NudgeConfigError(f"Braces in the message do not pair up ({exc}). "
                               "Use {first_name}, or {{ and }} for a literal brace.") from None
    names = []
    for _literal, field, spec, conversion in parsed:
        if field is None:
            continue
        if not field or spec or conversion or not field.isidentifier():
            raise NudgeConfigError(f"{{{field}{'!' + conversion if conversion else ''}"
                                   f"{':' + spec if spec else ''}}} is not a placeholder; "
                                   "write it as {name}.")
        names.append(field)
    return names


def check_placeholders(text: str, *, kind: str, has_coupon: bool, where: str) -> None:
    for name in placeholders_in(text):
        if name not in PLACEHOLDERS:
            raise NudgeConfigError(f"{where}: {{{name}}} is not a placeholder. "
                                   f"Available: {', '.join(sorted(PLACEHOLDERS))}.")
        if kind not in PLACEHOLDERS[name][1]:
            raise NudgeConfigError(f"{where}: {{{name}}} is not available for a "
                                   f"{kind} campaign.")
        if name in COUPON_PLACEHOLDERS and not has_coupon:
            raise NudgeConfigError(f"{where}: {{{name}}} needs a coupon on the campaign "
                                   "(promotion_code).")


# --- validation entry points ------------------------------------------------------------
def _merge(base: dict, override: dict) -> dict:
    """Deep merge: nested sections combine, anything else is replaced."""
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _explain(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        where = ".".join(str(p) for p in error["loc"]) or "value"
        message = error["msg"].removeprefix("Value error, ")
        if error["type"] == "extra_forbidden":
            message = "is not an option (check the spelling)"
        parts.append(f"{where}: {message}")
    return "; ".join(parts)


def parse_kind(raw) -> NudgeKind:
    try:
        return NudgeKind(str(raw or "").strip().lower())
    except ValueError:
        raise NudgeConfigError(f"kind must be one of {list(ALL_KINDS)}") from None


def build_campaign_config(kind: str, raw: dict | None, *, has_coupon: bool,
                          current: dict | None = None) -> CampaignConfig:
    """The campaign's config: kind defaults, then what is stored, then `raw`."""
    kind = parse_kind(kind)
    merged = _merge(_merge(KIND_DEFAULTS[kind], current or {}), raw or {})
    try:
        config = CampaignConfig.model_validate(merged)
    except ValidationError as exc:
        raise NudgeConfigError(_explain(exc)) from None

    allowed = set(TRIGGER_FIELDS[kind])
    used = {name for name, value in config.trigger.model_dump().items() if value is not None}
    stray = used - allowed
    if stray:
        raise NudgeConfigError(
            f"trigger: {sorted(stray)} do not apply to a {kind} campaign"
            + (f"; it uses {sorted(allowed)}" if allowed else "; it has no trigger options"))
    trigger = config.trigger
    if kind in (NudgeKind.ABANDONED_CART, NudgeKind.ABANDONED_CHECKOUT):
        if trigger.after_minutes is None or trigger.within_hours is None:
            raise NudgeConfigError("trigger: after_minutes and within_hours are required")
        if trigger.after_minutes >= trigger.within_hours * 60:
            raise NudgeConfigError("trigger: after_minutes must be less than within_hours")
    if kind == NudgeKind.REPEAT_PURCHASE:
        if trigger.days_since_last_order is None:
            raise NudgeConfigError("trigger: days_since_last_order is required")
        if (trigger.max_days_since_last_order is not None
                and trigger.max_days_since_last_order <= trigger.days_since_last_order):
            raise NudgeConfigError("trigger: max_days_since_last_order must be more than "
                                   "days_since_last_order")
    if kind == NudgeKind.NEVER_ORDERED:
        if trigger.days_since_signup is None:
            raise NudgeConfigError("trigger: days_since_signup is required")
        if (trigger.max_days_since_signup is not None
                and trigger.max_days_since_signup <= trigger.days_since_signup):
            raise NudgeConfigError("trigger: max_days_since_signup must be more than "
                                   "days_since_signup")

    message = config.message
    check_placeholders(message.text, kind=kind, has_coupon=has_coupon, where="message.text")
    for index, param in enumerate(message.template.params, start=1):
        check_placeholders(param, kind=kind, has_coupon=has_coupon,
                           where=f"message.template.params[{index}]")
    if message.template.always and not message.template.name:
        raise NudgeConfigError("message.template.always needs message.template.name")
    return config


def dump_config(kind: str, config: CampaignConfig) -> dict:
    """The config as stored and shown: JSON, with only this kind's trigger options."""
    data = config.model_dump(mode="json")
    data["trigger"] = {name: data["trigger"][name] for name in TRIGGER_FIELDS[parse_kind(kind)]}
    return data


def build_settings(raw: dict | None, *, current: dict | None = None) -> GlobalSettings:
    merged = _merge(GlobalSettings().model_dump(mode="json"), current or {})
    merged = _merge(merged, raw or {})
    try:
        return GlobalSettings.model_validate(merged)
    except ValidationError as exc:
        raise NudgeConfigError(_explain(exc)) from None


def reference() -> dict:
    """Everything a dashboard (or a person with curl) needs to build a campaign."""
    return {
        "kinds": {
            str(kind): {
                "description": KIND_DESCRIPTIONS[kind],
                "trigger_options": list(TRIGGER_FIELDS[kind]),
                "defaults": dump_config(kind, build_campaign_config(kind, None, has_coupon=True)),
            }
            for kind in NudgeKind
        },
        "placeholders": {name: {"description": text, "kinds": list(kinds)}
                         for name, (text, kinds) in PLACEHOLDERS.items()},
        "audience_filters": list(Audience.model_fields),
        "lead_stages": [str(s) for s in LeadStage],
        "days": list(DAYS),
        "settings_defaults": GlobalSettings().model_dump(mode="json"),
    }
