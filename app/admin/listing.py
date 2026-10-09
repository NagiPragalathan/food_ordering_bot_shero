"""Search, filters and pages for the admin's list screens.

Filters live in the URL (?q=...&status=...&page=2), so a filtered view can be
bookmarked, shared and reloaded. `url_with` builds the link for one change
- a tab, a sort, the next page - keeping every other filter as it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import Request


def url_with(request: Request, **changes) -> str:
    """This page's URL with some query parameters changed.

    A value of None or "" removes the parameter. Any change other than
    `page` drops the page number, so a new filter never opens on page 7.
    """
    params = dict(request.query_params)
    if any(key != "page" for key in changes):
        params.pop("page", None)
    for key, value in changes.items():
        if value is None or value == "":
            params.pop(key, None)
        else:
            params[key] = str(value)
    query = urlencode(params)
    return request.url.path + (f"?{query}" if query else "")


def choice(value: str | None, allowed: set[str] | dict, default: str) -> str:
    """A filter value from the URL, or the default if it is not one we know."""
    return value if value is not None and value in allowed else default


@dataclass(frozen=True)
class Page:
    """One page of a list: which rows to fetch, and what the pager shows."""

    number: int
    size: int
    total: int

    @classmethod
    def of(cls, requested: int | str | None, size: int, total: int) -> "Page":
        try:
            number = int(requested or 1)
        except (TypeError, ValueError):
            number = 1
        last = max(1, -(-total // size))           # ceiling division
        return cls(number=min(max(1, number), last), size=size, total=total)

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.size))

    @property
    def offset(self) -> int:
        return (self.number - 1) * self.size

    @property
    def first(self) -> int:
        return 0 if self.total == 0 else self.offset + 1

    @property
    def last(self) -> int:
        return min(self.total, self.offset + self.size)

    @property
    def has_prev(self) -> bool:
        return self.number > 1

    @property
    def has_next(self) -> bool:
        return self.number < self.pages


def ago(when: datetime | None, now: datetime | None = None) -> str:
    """'just now', '5 min ago', '3 h ago', '2 days ago', or a date."""
    if when is None:
        return "never"
    now = now or datetime.now(timezone.utc)
    when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)   # SQLite: naive UTC
    seconds = int((now - when).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    if seconds < 7 * 86400:
        days = seconds // 86400
        return f"{days} day{'s' if days > 1 else ''} ago"
    return when.strftime("%d %b %Y")
