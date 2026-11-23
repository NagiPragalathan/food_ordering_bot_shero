"""SFT's admin page: /admin/sft (Shero US). Not in the dashboard's menu yet:
open it by its address. One login with the rest of the dashboard.

    Overview    the KPIs, the funnel, sources, and who needs the team now
    Leads       every lead by stage; each lead's timeline and the team's actions
    Test chat   walk the journey as a lead would, without WhatsApp
    Journey     every message and button the bot sends, in order
    Settings    on/off/test, number, keywords, links, timings, team numbers
    Templates   the follow-up templates on WhatsApp, and submitting them
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import listing
from app.admin.deps import redirect, render, require_admin
from app.core import tenants
from app.core.config import settings
from app.core.exceptions import ConfigurationError, IntegrationError
from app.core.logging import get_logger
from app.db.models import AdminUser, SftEvent, SftLead
from app.db.session import get_session
from app.integrations.gallabox.client import use_channel
from app.integrations.gallabox.sender import use_sender
from app.schemas.inbound import InboundEvent, InboundKind
from app.sft import analytics, config, content as c, crm, flow, leads, nudges, payments
from app.sft import router as sft_router
from app.sft import sending
from app.sft.config import MODES, SftConfigError

log = get_logger(__name__)


def us_only() -> None:
    """SFT is Shero US's: there is no /dst/admin/sft."""
    if not tenants.current().is_default:
        raise HTTPException(404)


router = APIRouter(prefix="/sft", tags=["admin"], dependencies=[Depends(us_only)])

TABS = {"overview": "Overview", "leads": "Leads", "chat": "Test chat", "journey": "Journey",
        "settings": "Settings", "templates": "Templates"}
VIEWS = {"": "All", "attention": "Needs the team", "team": "With the team",
         **{k: v for k, v in c.STAGES.items()}, "test": "Test chat leads"}
PAGE_SIZE = 50
TEST_SOURCE = "Test chat"


def _url(request: Request, tab: str = "overview", **query) -> str:
    params = urlencode({"tab": tab, **{k: v for k, v in query.items() if v}})
    return f"{request.url_for('admin_sft')}?{params}"


def _local(cfg, when) -> str:
    when = leads.aware(when)
    if when is None:
        return ""
    return when.astimezone(ZoneInfo(cfg.timezone)).strftime("%d %b, %I:%M %p")


@router.get("", name="admin_sft")
async def sft_page(request: Request, tab: str = "overview", period: str = "30",
                   view: str = "", q: str = "", page: int | str | None = None,
                   number: str = "", session: AsyncSession = Depends(get_session),
                   current_user: AdminUser = Depends(require_admin)):
    tab = tab if tab in TABS else "overview"
    cfg = await config.load(session, fresh=True)
    context: dict = {"current_user": current_user, "tab": tab, "tabs": TABS, "cfg": cfg,
                     "modes": MODES, "c": c, "url": lambda t, **kw: _url(request, t, **kw),
                     "local": lambda when: _local(cfg, when), "missing": sft_router.ready(),
                     "zoho_connected": crm.connected(), "views": VIEWS}
    counts = dict((await session.execute(
        select(SftLead.stage, func.count()).where(SftLead.source != TEST_SOURCE)
        .group_by(SftLead.stage))).all())
    context["counts"] = counts
    context["attention_count"] = await _count(session, SftLead.needs_attention.is_(True))
    context["team_count"] = await _count(session, SftLead.with_team.is_(True))

    if tab == "overview":
        context["period"] = period if period in analytics.PERIODS else "30"
        context["periods"] = analytics.PERIODS
        context["report"] = await analytics.report(session, context["period"])
        context["queue"] = (await session.execute(
            select(SftLead).where(or_(SftLead.needs_attention.is_(True),
                                      SftLead.with_team.is_(True)))
            .order_by(SftLead.attention_priority.desc(), SftLead.attention_since)
            .limit(25))).scalars().all()
    elif tab == "leads":
        query = select(SftLead).where(SftLead.source != TEST_SOURCE) if view != "test"             else select(SftLead).where(SftLead.source == TEST_SOURCE)
        if view == "attention":
            query = query.where(SftLead.needs_attention.is_(True))
        elif view == "team":
            query = query.where(SftLead.with_team.is_(True))
        elif view in c.STAGES:
            query = query.where(SftLead.stage == view)
        if q.strip():
            term = f"%{q.strip()}%"
            query = query.where(or_(SftLead.name.ilike(term), SftLead.profile_name.ilike(term),
                                    SftLead.whatsapp_number.like(term),
                                    SftLead.source.ilike(term)))
        total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
        paged = listing.Page.of(page, PAGE_SIZE, total)
        context["leads"] = (await session.execute(
            query.order_by(SftLead.last_activity_at.desc()).offset(paged.offset)
            .limit(PAGE_SIZE))).scalars().all()
        context.update(view=view, q=q, page=paged,
                       url_with=lambda **kw: _url(request, "leads", view=view, q=q, **kw))
    elif tab == "chat":
        number = leads.digits(number) or "15550000001"
        lead = await leads.get(session, number)
        context.update(number=number, lead=lead,
                       chat=await _timeline(session, lead, kinds=("inbound", "sent",
                                                                  "send_failed", "not_sent"))
                       if lead else [],
                       last_buttons=await _last_buttons(session, lead) if lead else [])
    elif tab == "templates":
        context["templates"] = await _template_rows(cfg)
    return render(request, "admin/sft/page.html", context)


async def _count(session, condition) -> int:
    return await session.scalar(select(func.count()).select_from(SftLead).where(condition)) or 0


async def _timeline(session, lead, kinds: tuple[str, ...] | None = None, limit: int = 300):
    query = select(SftEvent).where(SftEvent.lead_id == lead.id)
    if kinds:
        query = query.where(SftEvent.kind.in_(kinds))
    rows = (await session.execute(query.order_by(SftEvent.at.desc()).limit(limit))).scalars().all()
    return list(reversed(rows))


async def _last_buttons(session, lead) -> list[list[str]]:
    row = (await session.execute(
        select(SftEvent).where(SftEvent.lead_id == lead.id, SftEvent.kind == "sent")
        .order_by(SftEvent.at.desc()).limit(1))).scalar_one_or_none()
    return list((row.data or {}).get("buttons") or []) if row else []


async def _template_rows(cfg) -> list[dict]:
    rows = []
    found: dict[str, dict] = {}
    error = ""
    try:
        from app.integrations.gallabox import template_admin

        with use_channel(cfg.channel_id or None):
            for row in await template_admin.list_templates(channel_id=cfg.channel_id or None):
                found[str(row.get("name"))] = row
    except (IntegrationError, ConfigurationError, Exception) as exc:  # noqa: BLE001
        error = str(exc)[:200]
    for spec in c.TEMPLATES:
        row = found.get(spec.name) or {}
        rows.append({"spec": spec, "status": str(row.get("status") or ("unknown" if error
                                                                          else "not submitted")),
                     "reason": row.get("rejectedReason") or row.get("reason") or "",
                     "error": error})
    return rows


# --- one lead -------------------------------------------------------------------------
async def _lead(session, lead_id: str) -> SftLead:
    try:
        lead = await session.get(SftLead, uuid.UUID(lead_id))
    except ValueError:
        lead = None
    if lead is None:
        raise HTTPException(404, "No such SFT lead")
    return lead


@router.get("/leads/{lead_id}", name="admin_sft_lead")
async def lead_page(lead_id: str, request: Request, session: AsyncSession = Depends(get_session),
                    current_user: AdminUser = Depends(require_admin)):
    lead = await _lead(session, lead_id)
    cfg = await config.load(session)
    drop = c.DROPS.get(lead.attention_stage or "")
    script = drop.script.format(first=leads.first(lead),
                                goal=c.GOALS.get(lead.goal or "", "SFT").lower()) if drop else ""
    return render(request, "admin/sft/lead.html", {
        "current_user": current_user, "lead": lead, "cfg": cfg, "c": c,
        "events": await _timeline(session, lead), "drop": drop, "script": script,
        "local": lambda when: _local(cfg, when),
        "enrol_url": flow.enrol_url(lead), "tags": sorted(crm.tags(lead)),
        "back": _url(request, "leads"),
    })


@router.post("/leads/{lead_id}/action", name="admin_sft_action")
async def lead_action(lead_id: str, request: Request,
                      session: AsyncSession = Depends(get_session),
                      current_user: AdminUser = Depends(require_admin)):
    lead = await _lead(session, lead_id)
    form = await request.form()
    action = str(form.get("action") or "")
    who = current_user.name or current_user.email
    here = str(request.url_for("admin_sft_lead", lead_id=lead_id))
    if action == "hand_back":
        lead.with_team = False
        lead.step = c.IDLE
        leads.touch(lead)
        await leads.resolve(session, lead, "answered by the team; back with the bot", by=who)
        await leads.event(session, lead, "team", f"{who} answered and handed the chat back to "
                          "the bot", crm=True)
        message = "Handed back to the bot: follow-ups resume from now."
    elif action == "take":
        lead.with_team = True
        lead.handed_over_at = leads.now()
        lead.step = c.WITH_TEAM
        await leads.event(session, lead, "team", f"{who} took the chat: the bot is silent",
                          crm=True)
        message = "The bot is silent for this lead until you hand it back."
    elif action in ("continue", "nurture7", "nurture30", "lost", "enrolled"):
        outcome = {"continue": "carry on with the journey", "nurture7": "follow up in a week",
                   "nurture30": "follow up in a month", "lost": "closed as lost",
                   "enrolled": "enrolled outside the bot"}[action]
        await leads.resolve(session, lead, outcome, by=who)
        if action.startswith("nurture"):
            days = 7 if action == "nurture7" else 30
            await leads.move(session, lead, c.NURTURE, f"{who}: {outcome}")
            lead.nurture_until = leads.now() + timedelta(days=days)
            lead.nurture_sent = 0
            lead.step = c.IDLE
        elif action == "lost":
            await leads.move(session, lead, c.LOST, f"{who}: {outcome}")
            lead.opted_out = True
            lead.with_team = False
            lead.step = c.IDLE
            reason = str(form.get("reason") or "").strip()[:60]
            if reason:
                lead.lost_reason = reason
        elif action == "enrolled":
            cfg = await config.load(session)
            await leads.event(session, lead, "team", f"{who} marked them enrolled (paid outside "
                              "the bot)", crm=True)
            with use_channel(lead.channel_id or cfg.channel_id or None):
                await payments.mark_paid(session, lead, cfg, amount=cfg.course_fee,
                                         currency=cfg.currency)
            lead.with_team = False
        else:
            leads.touch(lead)
            await leads.event(session, lead, "team", f"{who}: {outcome}", crm=True)
        message = f"Task closed: {outcome}."
    elif action == "notes":
        lead.notes = str(form.get("notes") or "").strip()[:5000] or None
        message = "Notes saved."
    elif action == "resync":
        lead.crm_dirty = True
        lead.crm_next_try_at = None
        lead.crm_attempts = 0
        message = "It goes to Zoho within a minute."
    else:
        return redirect(here, flash=("error", "Unknown action."))
    lead.crm_dirty = True
    return redirect(here, flash=("success", message))


# --- test chat ------------------------------------------------------------------------
class Collector:
    """Stands in for Gallabox in the test chat: nothing leaves the server."""

    async def send_text(self, *a, **k): return {}
    async def send_buttons(self, *a, **k): return {}
    async def send_list(self, *a, **k): return {}
    async def send_cta_url(self, *a, **k): return {}
    async def send_document(self, *a, **k): return {}
    async def send_template(self, *a, **k): return {}
    async def send_template_only(self, *a, **k): return {}
    async def handover_to_agent(self, *a, **k): return {}


@router.post("/chat", name="admin_sft_chat")
async def test_chat(request: Request, session: AsyncSession = Depends(get_session),
                    current_user: AdminUser = Depends(require_admin)):
    form = await request.form()
    number = leads.digits(str(form.get("number") or "")) or "15550000001"
    action = str(form.get("action") or "send")
    cfg = await config.load(session, fresh=True)
    back = _url(request, "chat", number=number)
    lead = await leads.get(session, number)
    if action == "reset":
        if lead is not None:
            if lead.source != TEST_SOURCE:
                return redirect(back, flash=("error", "Only a test-chat lead can be reset."))
            await session.delete(lead)
        return redirect(back, flash=("success", "Test lead deleted. Start again."))
    if lead is not None and lead.source != TEST_SOURCE:
        return redirect(back, flash=("error", "That number is a real lead. Use another number "
                                              "for the test chat."))
    with use_sender(Collector()):
        if action == "wait":
            hours = max(1, min(int(str(form.get("hours") or "24") or 24), 24 * 60))
            if lead is None:
                return redirect(back, flash=("error", "Say something first."))
            acted = await nudges.one(session, lead, cfg, leads.now() + timedelta(hours=hours))
            note = "something was sent" if acted else "nothing was due"
            return redirect(back, flash=("success", f"{hours} hours later: {note}."))
        if lead is None:
            lead = await leads.create(session, number, profile_name="Test Lead",
                                      source=TEST_SOURCE)
            lead.crm_dirty = False
        text = str(form.get("text") or "").strip()[:1000]
        button = str(form.get("button") or "")
        title = str(form.get("title") or "")
        if not text and not button:
            text = "SFT"
        ev = InboundEvent(whatsapp_number=number,
                          kind=InboundKind.REPLY if button else InboundKind.TEXT,
                          text=title if button else text, reply_id=button, reply_title=title,
                          contact_name="Test Lead", message_id=f"test-{uuid.uuid4().hex[:8]}")
        try:
            await flow.handle(session, ev, cfg, lead)
        except Exception as exc:  # noqa: BLE001 - show it, don't 500
            log.exception("sft_test_chat_failed")
            return redirect(back, flash=("error", f"The bot failed: {exc}"))
    lead.crm_dirty = False
    return redirect(back)


# --- settings, templates, Zoho ------------------------------------------------------
@router.post("/settings", name="admin_sft_settings")
async def save_settings(request: Request, session: AsyncSession = Depends(get_session),
                        current_user: AdminUser = Depends(require_admin)):
    form = await request.form()
    cfg = await config.load(session, fresh=True)
    data = cfg.model_dump(mode="json")
    for name in ("mode", "channel_id", "keywords", "ad_ids", "test_numbers", "team_numbers",
                 "brochure_url", "course_url", "course_access_note", "course_fee", "currency",
                 "followup_hours", "nurture_days", "payment_reminder_minutes",
                 "access_check_hours", "timezone", "send_from_hour", "send_until_hour",
                 "source_codes"):
        if name in form:
            data[name] = str(form.get(name) or "").strip()
    for flag in ("ask_name", "zoho_sync", "zoho_tasks"):
        data[flag] = form.get(flag) == "on"
    attention = dict(cfg.attention_hours)
    for key in attention:
        raw = str(form.get(f"attention_{key}") or "").strip()
        if raw:
            try:
                attention[key] = max(0.0, float(raw))
            except ValueError:
                return redirect(_url(request, "settings"),
                                flash=("error", f"Team hand-off for {key}: {raw!r} is not a number"))
    data["attention_hours"] = attention
    try:
        await config.save(session, data, current_user.email)
    except SftConfigError as exc:
        return redirect(_url(request, "settings"), flash=("error", str(exc)))
    sending.clear_cache()
    return redirect(_url(request, "settings"), flash=("success", "SFT settings saved."))


@router.post("/templates", name="admin_sft_templates")
async def submit_templates(request: Request, session: AsyncSession = Depends(get_session),
                           current_user: AdminUser = Depends(require_admin)):
    from app.integrations.gallabox import template_admin

    cfg = await config.load(session, fresh=True)
    back = _url(request, "templates")
    try:
        with use_channel(cfg.channel_id or None):
            existing = {str(r.get("name")) for r in
                        await template_admin.list_templates(channel_id=cfg.channel_id or None)}
            created = []
            for spec in c.TEMPLATES:
                if spec.name not in existing:
                    await template_admin.create_template(spec, channel_id=cfg.channel_id or None)
                    created.append(spec.name)
    except (IntegrationError, ConfigurationError, ValueError) as exc:
        return redirect(back, flash=("error", f"Gallabox refused: {exc}"))
    sending.clear_cache()
    message = (f"Submitted {len(created)} template(s) to WhatsApp for approval: "
               + ", ".join(created)) if created else "Every SFT template is already submitted."
    return redirect(back, flash=("success", message))


@router.post("/zoho", name="admin_sft_zoho")
async def zoho_actions(request: Request, session: AsyncSession = Depends(get_session),
                       current_user: AdminUser = Depends(require_admin)):
    form = await request.form()
    back = _url(request, "settings")
    try:
        if form.get("action") == "prepare":
            added = await crm.prepare(session)
            message = ("Added to Zoho's Lead Source: " + ", ".join(added)) if added else \
                "Zoho already has every SFT Lead Source."
        else:
            synced = await crm.run(session)
            message = f"Synced {synced} lead(s) to Zoho."
    except (IntegrationError, ConfigurationError) as exc:
        return redirect(back, flash=("error", f"Zoho: {exc}"))
    return redirect(back, flash=("success", message))
