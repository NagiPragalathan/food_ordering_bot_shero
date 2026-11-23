"""The date range the SFT admin pages are looking at: a preset (Today, Last 7
days…) or a custom From-To, in SFT's timezone. Each page filters on its own
date (when a lead came in, when they paid, when a nudge went)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

PRESETS: dict[str, str] = {
    "today": "Today", "yesterday": "Yesterday", "7": "Last 7 days", "30": "Last 30 days",
    "90": "Last 90 days", "month": "This month", "last_month": "Last month", "all": "All time",
}
CUSTOM = "custom"
DEFAULT = "30"
# A daily chart draws at most this many days.
CHART_DAYS = 92


@dataclass(frozen=True)
class Range:
    key: str
    label: str
    # In UTC: start included, end excluded; None is open-ended.
    start: datetime | None
    end: datetime | None
    # The same, as local dates (both included), for the date inputs.
    first: date | None
    last: date | None
    tz: str

    def query(self) -> dict[str, str]:
        """The URL parameters that bring this range back."""
        if self.key == CUSTOM:
            return {"range": CUSTOM, "from": _iso(self.first), "to": _iso(self.last)}
        return {"range": self.key}

    def has(self, when: datetime | None) -> bool:
        if when is None:
            return False
        when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
        return (self.start is None or when >= self.start) and (self.end is None or when < self.end)

    def where(self, column):
        """SQL conditions keeping `column` inside the range."""
        conditions = []
        if self.start is not None:
            conditions.append(column >= self.start)
        if self.end is not None:
            conditions.append(column < self.end)
        return conditions

    def days(self, earliest: date | None = None) -> list[date]:
        """The local days of the range, for a daily chart (the last CHART_DAYS)."""
        last = self.last or datetime.now(ZoneInfo(self.tz)).date()
        first = self.first or earliest or last - timedelta(days=29)
        first = max(first, last - timedelta(days=CHART_DAYS - 1))
        return [first + timedelta(days=i) for i in range((last - first).days + 1)]

    def day_of(self, when: datetime | None) -> date | None:
        if when is None:
            return None
        when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
        return when.astimezone(ZoneInfo(self.tz)).date()


def _iso(day: date | None) -> str:
    return day.isoformat() if day else ""


def _parse(text: str | None) -> date | None:
    try:
        return date.fromisoformat((text or "").strip()[:10])
    except ValueError:
        return None


def resolve(key: str | None, start: str | None = None, end: str | None = None, *,
            tz: str = "America/New_York", now: datetime | None = None,
            default: str = DEFAULT) -> Range:
    zone = ZoneInfo(tz)
    today = (now or datetime.now(timezone.utc)).astimezone(zone).date()

    def at(day: date) -> datetime:
        return datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc)

    key = (key or "").strip()
    if key == CUSTOM:
        first, last = _parse(start), _parse(end)
        if first is None and last is None:
            key = default
        else:
            if first and last and first > last:
                first, last = last, first
            label = (f"{first:%d %b %Y} – {last:%d %b %Y}" if first and last
                     else f"From {first:%d %b %Y}" if first else f"Until {last:%d %b %Y}")
            return Range(CUSTOM, label, at(first) if first else None,
                         at(last + timedelta(days=1)) if last else None, first, last, tz)
    if key not in PRESETS:
        key = default
    if key == "all":
        return Range(key, PRESETS[key], None, None, None, None, tz)
    if key == "today":
        first = last = today
    elif key == "yesterday":
        first = last = today - timedelta(days=1)
    elif key == "month":
        first, last = today.replace(day=1), today
    elif key == "last_month":
        last = today.replace(day=1) - timedelta(days=1)
        first = last.replace(day=1)
    else:
        first, last = today - timedelta(days=int(key) - 1), today
    return Range(key, PRESETS[key], at(first), at(last + timedelta(days=1)), first, last, tz)
