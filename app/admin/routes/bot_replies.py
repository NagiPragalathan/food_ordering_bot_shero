"""Bot replies: who the bot answers, and which messages start it.

Two independent settings, each saved from its own form:

  * Who: everyone, or only whitelisted WhatsApp numbers (BOT_REPLY_MODE,
    BOT_ALLOWED_NUMBERS; checked by services/allowlist.permits).
  * What: reply to any message, or only start on a trigger keyword
    (BOT_REPLY_TRIGGER, BOT_TRIGGER_KEYWORDS; services/reply_triggers.decide).

Both take effect at once (services/settings_store.py) and are checked on
every incoming WhatsApp message.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import redirect, render, require_admin
from app.core.logging import get_logger
from app.db.models import AdminUser, AppSetting
from app.db.session import get_session
from app.services import allowlist, reply_triggers
from app.services.settings_store import save_many

log = get_logger(__name__)
router = APIRouter(prefix="/bot-replies", tags=["admin"])


@router.get("", name="admin_bot_replies")
async def bot_replies_page(
    request: Request,
    check: str = "",
    message: str = "",
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    last = (await session.execute(
        select(AppSetting).where(AppSetting.key == "BOT_REPLY_MODE"))).scalar_one_or_none()
    return render(request, "admin/bot_replies.html", {
        "current_user": current_user,
        "mode": allowlist.mode(),
        "entries": allowlist.entries(),
        "last": last,
        "check": _check(check) if check.strip() else None,
        "check_query": check,
        "trigger_mode": reply_triggers.mode(),
        # The saved list even while switched off, so switching back keeps it.
        "triggers": reply_triggers.triggers(),
        "matches": reply_triggers.MATCHES,
        "active_hours": reply_triggers.ACTIVE_HOURS,
        "message_check": _message_check(message) if message.strip() else None,
        "message_query": message,
    })


@router.post("", name="admin_bot_replies_save")
async def save_bot_replies(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    url = str(request.url_for("admin_bot_replies"))
    form = await request.form()
    mode = str(form.get("mode") or "")
    if mode not in allowlist.MODES:
        return redirect(url, flash=("error", "Choose who the bot replies to."))

    rows = [(str(n).strip(), str(name)) for n, name
            in zip(form.getlist("number"), form.getlist("name"))]
    invalid = [n for n, _ in rows if n and not allowlist.is_valid(n)]
    if invalid:
        return redirect(url, flash=("error", "Not a full WhatsApp number (10 to 15 digits, "
                                             "with country code): " + ", ".join(invalid)))
    listed = allowlist.parse_entries(",".join(
        f"{n}|{allowlist.clean_name(name)}" for n, name in rows if n))
    if mode == allowlist.ALLOWLIST and not listed:
        # An empty whitelist would silence the bot for everyone.
        return redirect(url, flash=("error", "Add at least one WhatsApp number to the "
                                             "whitelist, or choose Reply to everyone."))

    values = {"BOT_REPLY_MODE": mode}
    if listed:      # an empty list is never stored: switching back keeps the old one
        values["BOT_ALLOWED_NUMBERS"] = allowlist.format_entries(listed)
    # Stored even when equal to .env, so this page alone decides from now on.
    changed = await save_many(session, values, updated_by=current_user.email,
                              allow_blank=set(values))

    log.info("bot_replies_saved", mode=mode, numbers=len(listed), changed=changed,
             by=current_user.email)
    if mode == allowlist.ALL:
        return redirect(url, flash=("success", "Live: the bot now replies to everyone."))
    return redirect(url, flash=("success", f"Test mode: the bot now replies only to "
                                           f"{len(listed)} whitelisted number(s)."))


@router.post("/triggers", name="admin_bot_triggers_save")
async def save_triggers(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: AdminUser = Depends(require_admin),
):
    url = str(request.url_for("admin_bot_replies"))
    form = await request.form()
    any_message = form.get("any_message") == "1"
    rows = [reply_triggers.Trigger(reply_triggers.clean_keyword(str(k)), str(match))
            for k, match in zip(form.getlist("keyword"), form.getlist("match"))
            if str(k).strip()]
    if any(t.match not in reply_triggers.MATCHES for t in rows):
        return redirect(url, flash=("error", "Choose how each keyword should match."))
    if len(rows) > reply_triggers.MAX_KEYWORDS:
        return redirect(url, flash=("error", f"At most {reply_triggers.MAX_KEYWORDS} keywords."))
    blank = [t.keyword for t in rows if not reply_triggers.normalise(t.keyword)]
    if blank:
        return redirect(url, flash=("error", "A keyword needs at least one letter or number: "
                                             + ", ".join(blank)))
    listed = reply_triggers.parse(reply_triggers.dump(rows))     # drops repeats
    if not any_message and not listed:
        # No trigger at all would mean the bot never starts for anyone.
        return redirect(url, flash=("error", "Add at least one trigger keyword, or switch "
                                             "Reply to any message back on."))

    values = {"BOT_REPLY_TRIGGER": reply_triggers.ANY if any_message else reply_triggers.KEYWORDS}
    if listed:      # an empty list is never stored: switching back on keeps the old one
        values["BOT_TRIGGER_KEYWORDS"] = reply_triggers.dump(listed)
    changed = await save_many(session, values, updated_by=current_user.email,
                              allow_blank=set(values))

    log.info("bot_triggers_saved", any_message=any_message, keywords=len(listed),
             changed=changed, by=current_user.email)
    if any_message:
        return redirect(url, flash=("success", "The bot now replies to any message."))
    return redirect(url, flash=("success", f"The bot now starts only on "
                                           f"{len(listed)} trigger keyword(s)."))


def _message_check(message: str) -> dict:
    """Would this message start the bot for a new customer, and why."""
    trigger = reply_triggers.matching(message)
    return {"any": reply_triggers.mode() == reply_triggers.ANY, "trigger": trigger,
            "built_in": reply_triggers.normalise(message) in reply_triggers.BUILT_IN,
            "match_label": reply_triggers.MATCHES.get(trigger.match, "") if trigger else ""}


def _check(number: str) -> dict:
    """Would the bot answer this number right now, and why."""
    if not allowlist.is_valid(number):
        return {"error": "Enter a full WhatsApp number, 10 to 15 digits with country code."}
    return {"number": "".join(ch for ch in number if ch.isdigit()),
            "replies": allowlist.permits(number), "entry": allowlist.match(number),
            "everyone": allowlist.mode() == allowlist.ALL}
