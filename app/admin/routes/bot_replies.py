"""Bot replies: answer everyone, or only whitelisted WhatsApp numbers.

Saved as the BOT_REPLY_MODE and BOT_ALLOWED_NUMBERS overrides, which take
effect at once (services/settings_store.py). The check itself is
services/allowlist.permits, run on every incoming WhatsApp message.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import redirect, render, require_admin
from app.core.logging import get_logger
from app.db.models import AdminUser, AppSetting
from app.db.session import get_session
from app.services import allowlist
from app.services.settings_store import save_many

log = get_logger(__name__)
router = APIRouter(prefix="/bot-replies", tags=["admin"])


@router.get("", name="admin_bot_replies")
async def bot_replies_page(
    request: Request,
    check: str = "",
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


def _check(number: str) -> dict:
    """Would the bot answer this number right now, and why."""
    if not allowlist.is_valid(number):
        return {"error": "Enter a full WhatsApp number, 10 to 15 digits with country code."}
    return {"number": "".join(ch for ch in number if ch.isdigit()),
            "replies": allowlist.permits(number), "entry": allowlist.match(number),
            "everyone": allowlist.mode() == allowlist.ALL}
