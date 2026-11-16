"""Comments on the dashboard's own pages: a reviewer points at an element and
asks for a change, much as in Figma (models/page_comment.py).

Who may comment: the admin logins switched on as reviewers on the Feedback
page; only they see the comment toggle. Anyone signed in can read the
comments there, reply, move them from Pending to In progress to Done, and
copy them.

A comment copies two ways:

  * as text - for a person: the page, the element and what was asked;
  * for AI - for a coding assistant: the same request with the exact URL,
    template file, route file, CSS selector and the element's HTML, so the
    change can be made without hunting for where it is.

Comments live in Shero US's database with the admin logins, whichever
business's page they were left on.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import tenants
from app.core.logging import get_logger
from app.db.models import CommentReviewer, PageComment

log = get_logger(__name__)

PENDING, IN_PROGRESS, DONE = "pending", "in_progress", "done"
STATUSES = {PENDING: "Pending", IN_PROGRESS: "In progress", DONE: "Done"}
MAX_BODY = 4000
MAX_HTML = 800
REVIEWERS_TTL = 60.0          # seconds a process trusts its copy of the list

_reviewers: set[str] = set()
_loaded_at: float = 0.0


class CommentError(ValueError):
    """A comment that cannot be saved, with the reason to show."""


# --- who may comment ---------------------------------------------------------------
def _email(raw: str) -> str:
    return (raw or "").strip().lower()


def can_comment(email: str | None) -> bool:
    """Does this admin see the comment toggle? (The list as last loaded.)"""
    return bool(email) and _email(email) in _reviewers


async def refresh_reviewers(session: AsyncSession, *, force: bool = False) -> None:
    """Reload the list, at most once a minute unless forced (require_admin
    calls this on every page, so a change reaches every page quickly)."""
    global _reviewers, _loaded_at
    if not force and time.monotonic() - _loaded_at < REVIEWERS_TTL:
        return
    rows = (await session.execute(select(CommentReviewer.email))).scalars()
    _reviewers = {_email(e) for e in rows}
    _loaded_at = time.monotonic()


async def reviewers(session: AsyncSession) -> list[CommentReviewer]:
    rows = await session.execute(select(CommentReviewer).order_by(CommentReviewer.email))
    return list(rows.scalars())


async def add_reviewer(session: AsyncSession, email: str, *, added_by: str) -> str:
    address = _email(email)
    if "@" not in address or len(address) > 255 or " " in address:
        raise CommentError("Enter an email address, like boss@shero.us.")
    if await session.get(CommentReviewer, address) is None:
        session.add(CommentReviewer(email=address, added_by=added_by))
        await session.flush()
    await refresh_reviewers(session, force=True)
    log.info("comment_reviewer_added", email=address, by=added_by)
    return address


async def remove_reviewer(session: AsyncSession, email: str, *, by: str) -> None:
    await session.execute(delete(CommentReviewer).where(CommentReviewer.email == _email(email)))
    await session.flush()
    await refresh_reviewers(session, force=True)
    log.info("comment_reviewer_removed", email=_email(email), by=by)


# --- threads -------------------------------------------------------------------------
@dataclass
class Thread:
    root: PageComment
    replies: list[PageComment] = field(default_factory=list)


async def threads(session: AsyncSession, *, path: str | None = None,
                  status: str | None = None, business: str | None = None) -> list[Thread]:
    """Comments with their replies, oldest first (the order pins are numbered in)."""
    query = select(PageComment).where(PageComment.parent_id.is_(None))
    if path is not None:
        query = query.where(PageComment.path == path)
    if status in STATUSES:
        query = query.where(PageComment.status == status)
    if business:
        query = query.where(PageComment.business == business)
    roots = list((await session.execute(query.order_by(PageComment.created_at,
                                                       PageComment.id))).scalars())
    if not roots:
        return []
    found = {r.id: Thread(r) for r in roots}
    replies = await session.execute(
        select(PageComment).where(PageComment.parent_id.in_(list(found)))
        .order_by(PageComment.created_at, PageComment.id))
    for reply in replies.scalars():
        found[reply.parent_id].replies.append(reply)
    return list(found.values())


async def thread(session: AsyncSession, comment_id: uuid.UUID) -> Thread:
    root = await session.get(PageComment, comment_id)
    if root is None or root.parent_id is not None:
        raise CommentError("That comment is gone.")
    found = await session.execute(select(PageComment).where(PageComment.parent_id == root.id)
                                  .order_by(PageComment.created_at, PageComment.id))
    return Thread(root, list(found.scalars()))


def _body(text: str) -> str:
    body = (text or "").strip()
    if not body:
        raise CommentError("Write what should change.")
    if len(body) > MAX_BODY:
        raise CommentError(f"Keep a comment under {MAX_BODY} characters.")
    return body


def _cut(value, limit: int) -> str | None:
    text = str(value or "").strip()
    return text[:limit] or None


def _fraction(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return min(max(number, 0.0), 1.0)


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def create(session: AsyncSession, data: dict, *, email: str, name: str | None) -> Thread:
    """A new comment pinned on a page element (data as the page sends it)."""
    path = _cut(data.get("path"), 500)
    if not path or not path.startswith("/"):
        raise CommentError("Which page is this comment on?")
    business = tenants.for_path(path).key
    comment = PageComment(
        business=business, path=path, query=_cut(data.get("query"), 500),
        page_title=_cut(data.get("page_title"), 200), template=_cut(data.get("template"), 200),
        route_file=_cut(data.get("route_file"), 200), selector=_cut(data.get("selector"), 2000),
        element_label=_cut(data.get("element_label"), 300),
        element_html=_cut(data.get("element_html"), MAX_HTML),
        x_pct=_fraction(data.get("x_pct")), y_pct=_fraction(data.get("y_pct")),
        page_x=_number(data.get("page_x")), page_y=_number(data.get("page_y")),
        viewport_width=int(_number(data.get("viewport_width")) or 0) or None,
        body=_body(data.get("body", "")), author_email=_email(email),
        author_name=_cut(name, 120), status=PENDING)
    session.add(comment)
    await session.flush()
    log.info("page_comment_added", path=path, by=_email(email))
    return Thread(comment)


async def reply(session: AsyncSession, comment_id: uuid.UUID, text: str, *,
                email: str, name: str | None) -> Thread:
    found = await thread(session, comment_id)
    root = found.root
    session.add(PageComment(
        parent_id=root.id, business=root.business, path=root.path, body=_body(text),
        author_email=_email(email), author_name=_cut(name, 120), status=PENDING))
    await session.flush()
    return await thread(session, comment_id)


async def set_status(session: AsyncSession, comment_id: uuid.UUID, status: str, *,
                     email: str) -> Thread:
    if status not in STATUSES:
        raise CommentError("A comment is Pending, In progress or Done.")
    found = await thread(session, comment_id)
    found.root.status = status
    found.root.resolved_by = _email(email) if status == DONE else None
    found.root.resolved_at = datetime.now(timezone.utc) if status == DONE else None
    await session.flush()
    return found


async def remove(session: AsyncSession, comment_id: uuid.UUID, *, email: str) -> None:
    comment = await session.get(PageComment, comment_id)
    if comment is None:
        return
    await session.execute(delete(PageComment).where(PageComment.parent_id == comment.id))
    await session.delete(comment)
    await session.flush()
    log.info("page_comment_deleted", path=comment.path, by=_email(email))


# --- the page a comment is on ----------------------------------------------------------
def source_of(request, template: str | None) -> dict:
    """What draws the page being rendered: its template and route files."""
    endpoint = request.scope.get("endpoint")
    module = getattr(endpoint, "__module__", "") or ""
    route_file = module.replace(".", "/") + ".py" if module.startswith("app.") else ""
    return {"template": f"app/templates/{template}" if template else "",
            "route_file": route_file}


def business_name(key: str) -> str:
    tenant = tenants.BY_KEY.get(key)
    return tenant.name if tenant else key


def page_name(comment: PageComment) -> str:
    """"Orders" out of "Orders · Shero US Admin"."""
    title = (comment.page_title or "").split("·")[0].strip()
    return title or comment.path


def _when(moment: datetime | None) -> str:
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC")


def label(status: str) -> str:
    return STATUSES.get(status, status.replace("_", " ").capitalize())


def _who(comment: PageComment) -> str:
    return comment.author_name or comment.author_email


# --- copying ---------------------------------------------------------------------------
def as_text(found: Thread) -> str:
    """For a person: the page, the element and what was asked."""
    root = found.root
    url = root.path + (root.query or "")
    lines = [f"{page_name(root)} page ({business_name(root.business)}) - {url}"]
    if root.element_label:
        lines.append(f"On: {root.element_label}")
    lines.append("")
    lines.append(f"{_who(root)}: {root.body}")
    for reply_ in found.replies:
        lines.append(f"{_who(reply_)}: {reply_.body}")
    lines.append("")
    lines.append(f"{label(root.status)} · {_when(root.created_at)}")
    return "\n".join(lines)


def for_ai(found: Thread) -> str:
    """For a coding assistant: the request with every path it needs."""
    root = found.root
    url = root.path + (root.query or "")
    lines = [
        "Change request for the Shero admin dashboard "
        "(FastAPI + Jinja2 templates + Tailwind CSS; repository food_ordering_bot_shero).",
        "",
        f"Page: {page_name(root)} ({business_name(root.business)})",
        f"URL path: {url}",
    ]
    if root.template:
        lines.append(f"Template file: {root.template}")
    if root.route_file:
        lines.append(f"Route file: {root.route_file}")
    if root.element_label:
        lines.append(f"Element: {root.element_label}")
    if root.selector:
        lines.append(f"CSS selector: {root.selector}")
    if root.element_html:
        lines += ["Element HTML (start):", root.element_html]
    lines += ["", f"Request from {_who(root)} ({_when(root.created_at)}):", root.body]
    if found.replies:
        lines += ["", "Discussion:"]
        lines += [f"- {_who(r)}: {r.body}" for r in found.replies]
    lines += [
        "",
        "Make only this change. The same templates serve both businesses - Shero US "
        "under /admin and DST India under /dst/admin - so keep both working, and leave "
        "Shero US's behaviour as it is unless the request says otherwise. Add or update "
        "tests for what changes.",
    ]
    return "\n".join(lines)


def serialize(found: Thread) -> dict:
    """A thread as the page script uses it."""
    root = found.root

    def person(comment: PageComment) -> dict:
        return {"email": comment.author_email, "name": _who(comment)}

    return {
        "id": str(root.id), "path": root.path, "query": root.query or "",
        "business": root.business, "page_title": root.page_title or "",
        "selector": root.selector or "", "element_label": root.element_label or "",
        "x_pct": root.x_pct, "y_pct": root.y_pct, "page_x": root.page_x, "page_y": root.page_y,
        "body": root.body, "author": person(root), "created_at": _when(root.created_at),
        "status": root.status, "status_label": label(root.status),
        "resolved_by": root.resolved_by or "",
        "replies": [{"id": str(r.id), "body": r.body, "author": person(r),
                     "created_at": _when(r.created_at)} for r in found.replies],
        "copy": {"ai": for_ai(found), "text": as_text(found)},
    }
