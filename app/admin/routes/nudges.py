"""Nudges: the targeting rules by stage, the No / Later / No response
sequences, the messages they send, and who got what. The run itself is in
services/nudges/runner.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import partial
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import listing
from app.admin.deps import redirect, render, require_admin
from app.core.exceptions import ConfigurationError, IntegrationError
from app.core.logging import get_logger
from app.db.models import AdminUser, Customer, NudgeBucket, NudgeLead, NudgeSend
from app.db.session import get_session
from app.services.nudges import config, messages
from app.services.nudges.config import BUCKETS, BUTTONS, STAGES, Message, NudgeConfigError

log = get_logger(__name__)
router = APIRouter(prefix="/nudges", tags=["admin"])

TABS = {"rules": "Targeting Rules", "sequence": "Retargeting Sequence", "messages": "Messages",
        "leads": "Leads"}
BUCKET_NAMES = {**BUCKETS, NudgeBucket.DEAD: "Dead Lead"}
PAGE_SIZE = 100


def _url(request: Request, tab: str, **query) -> str:
    params = urlencode({"tab": tab, **{k: v for k, v in query.items() if v}})
    return f"{request.url_for('admin_nudges')}?{params}"


def _local(cfg: config.NudgeSettings, when: datetime | None) -> str:
    if when is None:
        return ""
    when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)   # SQLite: naive UTC
    return when.astimezone(ZoneInfo(cfg.timezone)).strftime("%d %b, %I:%M %p")


def _int(value, default: int | None = None) -> int | None:
    text = str(value or "").strip()
    try:
        return int(text) if text else default
    except ValueError:
        raise NudgeConfigError(f"{text!r} is not a whole number") from None


def _in_use(cfg: config.NudgeSettings) -> set[str]:
    """Messages a rule or sequence that is on would send."""
    used = {r.message for r in cfg.rules if r.active and r.message}
    for seq in cfg.sequences.values():
        if seq.active:
            used |= {n for n in (seq.first_message, seq.follow_up_message) if n}
    return used


@router.get("", name="admin_nudges")
async def nudges_page(request: Request, tab: str = "rules", stage: str | None = None,
                      bucket: str | None = None, page: int | str | None = None,
                      fresh: bool = False, session: AsyncSession = Depends(get_session),
                      current_user: AdminUser = Depends(require_admin)):
    tab = tab if tab in TABS else "rules"
    cfg = await config.load(session)
    statuses = await messages.statuses(fresh=fresh)
    # Not approved yet (or refused): the page counts them, and warns when a
    # rule that is on would send one.
    waiting = [m.name for m in cfg.messages
               if statuses is not None and statuses.get(m.name) != "approved"]
    context = {"current_user": current_user, "tab": tab, "tabs": TABS, "cfg": cfg,
               "statuses": statuses, "stages": STAGES, "buckets": BUCKETS,
               "bucket_names": BUCKET_NAMES, "preview": messages.preview, "buttons": BUTTONS,
               "hours": range(0, 25), "waiting": waiting,
               "blocked": sorted(_in_use(cfg) & set(waiting))}
    if tab == "messages":
        context["addable"] = await messages.addable({m.name for m in cfg.messages})
    if tab == "leads":
        context.update(await _leads(session, cfg, stage=stage, bucket=bucket, page=page))
        context["url_with"] = partial(listing.url_with, request)
    return render(request, "admin/nudges.html", context)


async def _leads(session: AsyncSession, cfg, *, stage, bucket, page) -> dict:
    stage = stage if stage in STAGES else None
    bucket = bucket if bucket in BUCKET_NAMES else None
    in_stage = dict((await session.execute(
        select(Customer.lead_stage, func.count()).where(Customer.nudges_opted_out_at.is_(None))
        .group_by(Customer.lead_stage))).all())
    sent_in_stage = dict((await session.execute(
        select(NudgeLead.stage, func.count()).where(NudgeLead.bucket.is_(None),
                                                    NudgeLead.stage_sent_at.is_not(None))
        .group_by(NudgeLead.stage))).all())
    in_bucket = dict((await session.execute(
        select(NudgeLead.bucket, func.count()).where(NudgeLead.bucket.is_not(None))
        .group_by(NudgeLead.bucket))).all())

    query = select(Customer, NudgeLead).join(NudgeLead, NudgeLead.customer_id == Customer.id)
    if stage:
        query = query.where(NudgeLead.stage == stage, NudgeLead.bucket.is_(None))
    if bucket:
        query = query.where(NudgeLead.bucket == bucket)
    total = (await session.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    pager = listing.Page.of(page, PAGE_SIZE, total)
    rows = (await session.execute(query.order_by(NudgeLead.updated_at.desc())
                                  .offset(pager.offset).limit(PAGE_SIZE))).all()
    ids = [c.id for c, _ in rows]
    last: dict = {}
    if ids:
        for send in (await session.execute(select(NudgeSend).where(NudgeSend.customer_id.in_(ids))
                                           .order_by(NudgeSend.sent_at))).scalars():
            last[send.customer_id] = send
    log_query = select(NudgeSend, Customer).join(Customer, Customer.id == NudgeSend.customer_id)
    if stage:
        log_query = log_query.where(NudgeSend.stage == stage, NudgeSend.kind == "stage")
    if bucket:
        log_query = log_query.where(NudgeSend.kind == bucket)
    sends = (await session.execute(log_query.order_by(NudgeSend.sent_at.desc()).limit(100))).all()
    return {"in_stage": in_stage, "sent_in_stage": sent_in_stage, "in_bucket": in_bucket,
            "rows": rows, "last": last, "pager": pager, "sends": sends, "stage": stage,
            "bucket": bucket, "local": lambda when: _local(cfg, when)}


async def _save(request: Request, session, data: dict, actor: str, tab: str, done: str):
    try:
        await config.save(session, data, actor)
    except NudgeConfigError as exc:
        return redirect(_url(request, tab), flash=("error", f"Not saved: {exc}"))
    log.info("nudges_saved", by=actor, tab=tab)
    return redirect(_url(request, tab), flash=("success", done))


@router.post("/settings", name="admin_nudges_settings")
async def save_settings(request: Request, session: AsyncSession = Depends(get_session),
                        current_user: AdminUser = Depends(require_admin)):
    form = await request.form()
    tab = str(form.get("tab") or "rules")
    cfg = await config.load(session)
    try:
        data = cfg.model_dump() | {
            "enabled": form.get("enabled") == "on", "zoho_sync": form.get("zoho_sync") == "on",
            "send_from": _int(form.get("send_from"), cfg.send_from),
            "send_to": _int(form.get("send_to"), cfg.send_to),
            "timezone": str(form.get("timezone") or cfg.timezone).strip()}
    except NudgeConfigError as exc:
        return redirect(_url(request, tab), flash=("error", f"Not saved: {exc}"))
    return await _save(request, session, data, current_user.email, tab,
                       "Nudges are on." if data["enabled"] else "Nudges are off: nothing will be sent.")


@router.post("/rules", name="admin_nudges_rules")
async def save_rules(request: Request, session: AsyncSession = Depends(get_session),
                     current_user: AdminUser = Depends(require_admin)):
    form = await request.form()
    cfg = await config.load(session)
    try:
        rules = [{"stage": s, "active": form.get(f"active_{i}") == "on",
                  "wait_hours": _int(form.get(f"wait_{i}"), 24),
                  "message": str(form.get(f"message_{i}") or "") or None,
                  "no_response_hours": _int(form.get(f"no_response_{i}"))}
                 for i, s in enumerate(STAGES)]
    except NudgeConfigError as exc:
        return redirect(_url(request, "rules"), flash=("error", f"Not saved: {exc}"))
    return await _save(request, session, cfg.model_dump() | {"rules": rules}, current_user.email,
                       "rules", "Targeting rules saved.")


@router.post("/sequences", name="admin_nudges_sequences")
async def save_sequences(request: Request, session: AsyncSession = Depends(get_session),
                         current_user: AdminUser = Depends(require_admin)):
    form = await request.form()
    cfg = await config.load(session)
    sequences = {}
    try:
        for b in BUCKETS:
            phases = []
            for k in range(3):
                every = _int(form.get(f"{b}_every_{k}"))
                if every:
                    phases.append({"every_days": every, "times": _int(form.get(f"{b}_times_{k}"))})
            sequences[b] = {
                "active": form.get(f"{b}_active") == "on",
                "first_after": _int(form.get(f"{b}_first_after"), 1),
                "first_unit": str(form.get(f"{b}_first_unit") or "days"),
                "first_message": str(form.get(f"{b}_first_message") or "") or None,
                "follow_up_message": str(form.get(f"{b}_follow_up_message") or "") or None,
                "phases": phases,
                "dead_after_days": _int(form.get(f"{b}_dead_days")),
                "dead_after_messages": _int(form.get(f"{b}_dead_messages")),
                "reply": str(form.get(f"{b}_reply") or "").strip(),
            }
    except NudgeConfigError as exc:
        return redirect(_url(request, "sequence"), flash=("error", f"Not saved: {exc}"))
    return await _save(request, session, cfg.model_dump() | {"sequences": sequences},
                       current_user.email, "sequence", "Retargeting sequences saved.")


@router.post("/messages", name="admin_nudges_message_add")
async def add_message(request: Request, session: AsyncSession = Depends(get_session),
                      current_user: AdminUser = Depends(require_admin)):
    form = await request.form()
    cfg = await config.load(session)
    label, text = str(form.get("label") or "").strip(), str(form.get("text") or "").strip()
    if not label:
        return redirect(_url(request, "messages"), flash=("error", "Give the message a name."))
    known = await messages.statuses(fresh=True) or {}
    name = messages.name_for(label, {m.name for m in cfg.messages} | set(known))
    try:
        message = Message(name=name, text=text, label=label[:80], created_at=datetime.now(timezone.utc),
                          created_by=current_user.email)
    except ValueError as exc:
        return redirect(_url(request, "messages"),
                        flash=("error", f"Not saved: {config.readable(exc)}"))
    try:
        status = await messages.submit(message)
    except (IntegrationError, ConfigurationError, ValueError) as exc:
        return redirect(_url(request, "messages"), flash=(
            "error", f"WhatsApp did not take the message, so it was not saved: {exc}"))
    return await _save(request, session, cfg.model_dump() | {
        "messages": [*cfg.model_dump()["messages"], message.model_dump()]},
        current_user.email, "messages",
        f"Saved {name} and sent it to WhatsApp for approval ({status}). It can be used in a "
        f"rule once approved, usually within a few minutes to a day.")


@router.get("/statuses", name="admin_nudges_statuses")
async def message_statuses(session: AsyncSession = Depends(get_session),
                           current_user: AdminUser = Depends(require_admin)):
    """Each listed message's approval, asked of Gallabox now: the page polls
    this while a message waits, so its badge turns Approved by itself."""
    cfg = await config.load(session)
    statuses = await messages.statuses(fresh=True)
    if statuses is None:
        return JSONResponse({"statuses": None})
    return JSONResponse({"statuses": {m.name: statuses.get(m.name, "missing")
                                      for m in cfg.messages}})


@router.post("/messages/from-whatsapp", name="admin_nudges_message_import")
async def add_from_whatsapp(request: Request, session: AsyncSession = Depends(get_session),
                            current_user: AdminUser = Depends(require_admin)):
    """Add approved templates already on WhatsApp (the SMO messages) to the list."""
    form = await request.form()
    cfg = await config.load(session)
    addable = await messages.addable({m.name for m in cfg.messages})
    if addable is None:
        return redirect(_url(request, "messages"), flash=(
            "error", "Gallabox could not be reached, so nothing was added. Try again shortly."))
    wanted = set(form.getlist("names"))
    chosen = addable if form.get("all") == "on" else [m for m in addable if m["name"] in wanted]
    if not chosen:
        return redirect(_url(request, "messages"), flash=("error", "Tick the messages to add."))
    now = datetime.now(timezone.utc)
    data = cfg.model_dump()
    data["messages"] = [*data["messages"], *({**m, "created_at": now,
                                              "created_by": current_user.email} for m in chosen)]
    return await _save(request, session, data, current_user.email, "messages",
                       f"Added {len(chosen)} approved message{'s' if len(chosen) != 1 else ''} "
                       f"from WhatsApp. Choose them in your rules and sequences.")


@router.post("/messages/{name}/remove", name="admin_nudges_message_remove")
async def remove_message(request: Request, name: str, session: AsyncSession = Depends(get_session),
                         current_user: AdminUser = Depends(require_admin)):
    cfg = await config.load(session)
    used = cfg.uses(name)
    if used:
        return redirect(_url(request, "messages"), flash=(
            "error", f"{name} is used by {', '.join(used)}. Choose another message there first."))
    data = cfg.model_dump()
    data["messages"] = [m for m in data["messages"] if m["name"] != name]
    return await _save(request, session, data, current_user.email, "messages",
                       f"Removed {name} from the list. The template stays in Gallabox, "
                       f"which cannot delete it; delete it there if you want it gone.")
