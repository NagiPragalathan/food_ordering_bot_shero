"""Each library message's WhatsApp template: submitted to Meta (through
Gallabox) when the message is saved, its approval shown on the Messages tab,
and removed when the message is deleted.

Outside WhatsApp's 24-hour window only an approved template reaches a
customer, so every message gets one, named after it (`smo_m1_01` for M1-01
in the SMO pool), category Marketing, with the message's text - a {placeholder}
becomes {{1}}, {{2}}... and is filled in when it is sent.

Gallabox's API creates and lists templates but cannot edit or delete one
(PUT and DELETE both 404; template_admin.py). So:

* changed text is a new template - the same name with _v2, _v3... The message
  waits for that one's approval: the old template would send the old words,
  so it is no longer used (`matches`) and is listed to remove;
* a deleted message's template is deleted through Meta's own API when
  META_SYSTEM_USER_TOKEN and META_WABA_ID are set; otherwise it is listed on
  the Messages tab to delete in Gallabox (`leftovers`). Either way it is never
  sent again: only a message in the library is ever sent.

Gallabox's list is read at most once a minute; a save reads it afresh.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from string import Formatter

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import is_unset, settings
from app.core.exceptions import ConfigurationError, IntegrationError
from app.core.logging import get_logger
from app.db.models import AppSetting, NudgeMessage
from app.integrations.gallabox import template_admin
from app.integrations.gallabox.templates import TemplateSpec

log = get_logger(__name__)

CACHE_SECONDS = 60
CATEGORY = "MARKETING"
# Templates left on the WhatsApp account by deleted or rewritten messages.
LEFTOVERS_KEY = "NUDGE_TEMPLATE_LEFTOVERS"
GALLABOX_TEMPLATES = "https://app.gallabox.com/templates"

PENDING = {"pending", "in_review", "in_appeal", "submitted", "pending_deletion"}
REFUSED = {"rejected", "error", "disabled", "paused", "flagged", "failed"}

# What a message's template means for sending, shown on the Messages tab.
STATES = {
    "approved": "Approved - goes to anyone, any time (not US numbers outside 24 h)",
    "pending": "Waiting for Meta's approval",
    "rejected": "Rejected by Meta",
    "text_changed": "Text changed since it was submitted",
    "not_submitted": "Not submitted",
    "none": "No template",
    "unknown": "Gallabox could not be asked",
}

_cache: dict = {"at": 0.0, "rows": None}


# --- what Gallabox has -------------------------------------------------------------------------
async def rows(*, fresh: bool = False) -> dict[str, dict] | None:
    """Every template on the account by name (this channel's first), or None
    when Gallabox cannot be asked."""
    if not fresh and _cache["rows"] is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS:
        return _cache["rows"]
    try:
        found = await template_admin.list_templates(channel_id="")
    except (ConfigurationError, IntegrationError, httpx.HTTPError) as exc:
        log.warning("nudge_templates_unavailable", error=str(exc))
        return None
    channel = settings.gallabox_channel_id
    by_name: dict[str, dict] = {}
    # Another channel's template only holds the name; ours says what we can send.
    for row in sorted(found, key=lambda r: r.get("channelId") == channel):
        if row.get("name"):
            by_name[str(row["name"])] = {**row, "ours": row.get("channelId") == channel}
    _cache.update(at=time.monotonic(), rows=by_name)
    return by_name


def forget() -> None:
    _cache.update(at=0.0, rows=None)


def body_of(row: dict) -> str:
    for component in row.get("components") or []:
        if isinstance(component, dict) and str(component.get("type") or "").upper() == "BODY":
            return str(component.get("text") or "")
    return str(row.get("body") or "")


def _same(a: str, b: str) -> bool:
    return " ".join((a or "").split()) == " ".join((b or "").split())


def template_text(body: str) -> tuple[str, list[str]]:
    """The message as a template body: each {placeholder} becomes {{1}}, {{2}}...
    in order, and the placeholders are the values to fill in when sending."""
    out, params = [], []
    for literal, field, _spec, _conv in Formatter().parse(body or ""):
        out.append(literal)
        if field is not None:
            params.append("{" + field + "}")
            out.append("{{%d}}" % len(params))
    return "".join(out).strip(), params


def default_name(message: NudgeMessage) -> str:
    """smo_m1_01 for M1-01 in the SMO pool: lower case, digits and _ only."""
    name = re.sub(r"[^a-z0-9]+", "_", f"{message.pool}_{message.code}".lower()).strip("_")
    return name[:100] or "nudge_message"


def _next_version(name: str, taken: dict) -> str:
    base = re.sub(r"_v\d+$", "", name)
    number = 2
    while f"{base}_v{number}" in taken:
        number += 1
    return f"{base}_v{number}"


def state(message: NudgeMessage, found: dict[str, dict] | None) -> dict:
    """Where the message's template stands, for the Messages tab."""
    name = message.wa_template
    if not name:
        key = "none"
    elif found is None:
        key = "unknown"
    else:
        row = found.get(name)
        status = str((row or {}).get("status") or "").lower()
        if row is None:
            key = "not_submitted"
        elif status in REFUSED:
            key = "rejected"
        elif not _same(body_of(row), template_text(message.body)[0]):
            key = "text_changed"
        elif status == "approved":
            key = "approved"
        else:
            key = "pending"
    view = {"state": key, "label": STATES[key], "name": name}
    row = (found or {}).get(name or "")
    if row:
        view.update(status=str(row.get("status") or ""), category=row.get("category"),
                    reason=_reason(row), updated_at=row.get("statusUpdatedAt"))
    return view


def _reason(row: dict) -> str | None:
    for key in ("rejected_reason", "rejectedReason", "reason", "err", "error"):
        value = row.get(key)
        if value and str(value).upper() != "NONE":
            return str(value)[:300]
    return None


def summary(views: list[dict]) -> dict:
    counts = {key: 0 for key in STATES}
    for view in views:
        counts[view["state"]] += 1
    return counts


async def matches(message: NudgeMessage) -> bool:
    """Its template still has the message's words. A template whose text no
    longer matches would send the old words, so it is not used. Unknown
    (Gallabox not reachable) counts as a match: approval is checked anyway."""
    found = await rows()
    row = (found or {}).get(message.wa_template or "")
    return row is None or _same(body_of(row), template_text(message.body)[0])


# --- submitting ---------------------------------------------------------------------------------
async def submit(session: AsyncSession, message: NudgeMessage, *, actor: str) -> dict:
    """Make sure Meta has this message's text as a template, submitting it
    (or a new version of it) when not. Never raises: the result says what
    happened, and the message is saved either way."""
    if not (message.body or "").strip():
        return {"submitted": False, "error": "The message has no text."}
    found = await rows(fresh=True)
    if found is None:
        return {"submitted": False, "error": "Gallabox could not be reached; press Submit "
                                             "for approval on the message to try again."}
    text, params = template_text(message.body)
    name = message.wa_template or default_name(message)
    row = found.get(name)
    if row is not None:
        status = str(row.get("status") or "").lower()
        if _same(body_of(row), text) and status not in REFUSED:
            return {"submitted": False, "already": True, "name": name, "status": status}
        if message.wa_template == name and not await _shared(session, name, message):
            await add_leftover(session, name, message.code,
                               "rejected by Meta, replaced by a new version" if status in REFUSED
                               else "text changed, replaced by a new version")
        name = _next_version(name, found)
    spec = TemplateSpec(
        name=name, category=CATEGORY, trigger=f"Nudge message {message.code}",
        params=tuple(p.strip("{}") for p in params), sample_body=text,
        button_kind="dynamic_url" if message.wa_button == "order_link" else "none",
        button_label="Order now", url_base="order")
    try:
        record = await template_admin.create_template(spec)
    except (ConfigurationError, IntegrationError, httpx.HTTPError, ValueError) as exc:
        log.warning("nudge_template_submit_failed", code=message.code, name=name, error=str(exc))
        return {"submitted": False, "name": name, "error": str(exc)[:300]}
    message.wa_template, message.wa_params = name, params
    message.updated_by = actor
    await session.flush()
    forget()
    log.info("nudge_template_submitted", code=message.code, name=name, actor=actor,
             status=record.get("status"))
    return {"submitted": True, "name": name, "status": str(record.get("status") or "pending")}


def needs_submitting(message: NudgeMessage, found: dict[str, dict] | None) -> bool:
    return found is not None and state(message, found)["state"] in (
        "none", "not_submitted", "text_changed")


async def submit_missing(session: AsyncSession, *, actor: str) -> int:
    """Submit every library message whose template is missing or out of date
    (after an import). Returns how many were submitted."""
    found = await rows(fresh=True)
    count = 0
    for message in (await session.execute(select(NudgeMessage).order_by(NudgeMessage.code))).scalars():
        if needs_submitting(message, found):
            result = await submit(session, message, actor=actor)
            count += bool(result.get("submitted"))
            found = await rows()
    return count


# --- removing -----------------------------------------------------------------------------------
def can_delete() -> bool:
    return not (is_unset(settings.meta_system_user_token) or is_unset(settings.meta_waba_id))


async def remove(session: AsyncSession, message: NudgeMessage) -> dict:
    """The deleted message's template: deleted through Meta when the keys are
    set, otherwise listed to delete in Gallabox. Kept if another message uses it."""
    name = message.wa_template
    if not name:
        return {"template": None}
    if await _shared(session, name, message):
        return {"template": name, "deleted": False, "kept": "another message uses it"}
    if can_delete():
        error = await _meta_delete(name)
        if error is None:
            forget()
            log.info("nudge_template_deleted", name=name, code=message.code)
            return {"template": name, "deleted": True}
        await add_leftover(session, name, message.code, f"message deleted; Meta said: {error}")
        return {"template": name, "deleted": False, "error": error}
    await add_leftover(session, name, message.code, "message deleted")
    return {"template": name, "deleted": False, "listed": True}


async def _shared(session: AsyncSession, name: str, message: NudgeMessage) -> bool:
    """Another message uses this template too."""
    return bool((await session.execute(select(func.count()).select_from(NudgeMessage).where(
        NudgeMessage.wa_template == name, NudgeMessage.id != message.id))).scalar_one())


async def _meta_delete(name: str) -> str | None:
    """Delete a template by name with Meta's Graph API. None when done."""
    url = (f"https://graph.facebook.com/{settings.meta_graph_version}/"
           f"{settings.meta_waba_id}/message_templates")
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.delete(url, params={"name": name}, headers={
                "Authorization": f"Bearer {settings.meta_system_user_token}"})
    except httpx.HTTPError as exc:
        return str(exc)[:200]
    if response.status_code >= 400:
        return response.text[:200]
    return None


async def _leftover_row(session: AsyncSession) -> AppSetting | None:
    return (await session.execute(
        select(AppSetting).where(AppSetting.key == LEFTOVERS_KEY))).scalar_one_or_none()


async def leftovers(session: AsyncSession, found: dict[str, dict] | None = None) -> list[dict]:
    """Templates no message uses any more, to delete in Gallabox. One that is
    gone from Gallabox drops off by itself."""
    row = await _leftover_row(session)
    items = json.loads(row.value) if row and row.value else []
    if found is not None:
        kept = [item for item in items if item["name"] in found]
        if len(kept) != len(items):
            row.value = json.dumps(kept)
            await session.flush()
        items = kept
    return items


async def add_leftover(session: AsyncSession, name: str, code: str, why: str) -> None:
    row = await _leftover_row(session)
    if row is None:
        row = AppSetting(key=LEFTOVERS_KEY, is_secret=False, value="[]")
        session.add(row)
    items = [item for item in json.loads(row.value or "[]") if item["name"] != name]
    items.append({"name": name, "code": code, "why": why,
                  "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    row.value = json.dumps(items)
    await session.flush()


async def dismiss(session: AsyncSession, name: str) -> None:
    row = await _leftover_row(session)
    if row is not None:
        row.value = json.dumps([i for i in json.loads(row.value or "[]") if i["name"] != name])
        await session.flush()
