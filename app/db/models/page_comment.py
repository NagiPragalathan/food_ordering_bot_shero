"""Comments people leave on the dashboard's own pages (services/page_comments.py).

A reviewer turns on comment mode, clicks any element of a page and writes
what should change there - a feature, a UI fix - much as in Figma. Each
comment remembers the page (its URL, the template and the route file that
draw it) and the element (a CSS selector, its text, where on it the click
was), so it can be pinned back on the page and copied as an exact request.

Kept in Shero US's database with the admin logins, whichever business's
page it was left on (`business` says which).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey


class PageComment(Base, UUIDPrimaryKey, Timestamps):
    """A comment pinned on a page, or a reply to one (`parent_id`)."""

    __tablename__ = "page_comments"

    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("page_comments.id", ondelete="CASCADE"), index=True)
    business: Mapped[str] = mapped_column(String(20), nullable=False, default="us")
    # Where: /admin/orders or /dst/admin/orders, and its query string.
    path: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    query: Mapped[str | None] = mapped_column(String(500))
    page_title: Mapped[str | None] = mapped_column(String(200))
    # What draws it: app/templates/admin/orders.html, app/admin/routes/orders.py.
    template: Mapped[str | None] = mapped_column(String(200))
    route_file: Mapped[str | None] = mapped_column(String(200))
    # The element: how to find it again, and what it said.
    selector: Mapped[str | None] = mapped_column(Text)
    element_label: Mapped[str | None] = mapped_column(String(300))
    element_html: Mapped[str | None] = mapped_column(Text)
    # Where on the element the click was (0-1), and on the page as a fallback.
    x_pct: Mapped[float | None] = mapped_column(Float)
    y_pct: Mapped[float | None] = mapped_column(Float)
    page_x: Mapped[float | None] = mapped_column(Float)
    page_y: Mapped[float | None] = mapped_column(Float)
    viewport_width: Mapped[int | None] = mapped_column(Integer)

    body: Mapped[str] = mapped_column(Text, nullable=False)
    author_email: Mapped[str] = mapped_column(String(255), nullable=False)
    author_name: Mapped[str | None] = mapped_column(String(120))
    # A thread's state, on its first comment: pending, in_progress or done.
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="pending")
    resolved_by: Mapped[str | None] = mapped_column(String(255))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CommentReviewer(Base, Timestamps):
    """An admin email that may comment on the pages (sees the toggle)."""

    __tablename__ = "page_comment_reviewers"

    email: Mapped[str] = mapped_column(String(255), primary_key=True)
    added_by: Mapped[str | None] = mapped_column(String(255))
