"""SFT's admin pages: /admin/sft (Shero US). The dashboard's top tabs open it:
Shero US | DST India | Shero Food Training (admin/deps.py tenant_tabs), and the
same tabs on its pages lead back. One login with the rest of the dashboard;
its own layout (admin/sft/base.html) with only SFT's sections.

    Overview     the KPIs, the funnel and a daily chart for the chosen dates
    Leads        every lead: a list, or a board by stage; each lead's page
    Customers    everyone who paid: payments, revenue, refunds, receipts, who
                 is waiting to pay, and each student's course access
    Nudges       the automated follow-ups: rules, who is in which flow, what was
                 sent, and the WhatsApp templates
    Offers       codes and launch prices for the course
    Bot replies  who SFT answers, what starts it, and everything it says
    Test chat    walk the journey as a lead would, without WhatsApp
    Settings     the course, the brochure, the team's numbers, Zoho

The Overview, Leads, Customers and Nudges take a date range
(app/sft/periods.py), kept as the sidebar moves between them.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import listing
from app.admin.deps import redirect, render, require_admin
from app.core import tenants
from app.core.config import is_unset, settings
from app.core.exceptions import ConfigurationError, IntegrationError
from app.core.logging import get_logger
from app.db.models import AdminUser, SftEvent, SftLead, SftOffer
from app.db.session import get_session
from app.integrations.gallabox.client import use_channel
from app.integrations.gallabox.sender import use_sender
from app.schemas.inbound import InboundEvent, InboundKind
from app.sft import analytics, brochure, config, content as c, crm, flow, leads, nudges, offers
from app.sft import payments, receipt
from app.sft import periods, sending, views
from app.sft import router as sft_router
from app.sft.config import MODES, NUDGES, SftConfigError

log = get_logger(__name__)


def us_only() -> None:
    """SFT is Shero US's: there is no /dst/admin/sft."""
    if not tenants.current().is_default:
        raise HTTPException(404)


router = APIRouter(prefix="/sft", tags=["admin"], dependencies=[Depends(us_only)])

TABS = {"overview": "Overview", "leads": "Leads", "customers": "Customers",
        "nudges": "Nudges", "offers": "Offers", "replies": "Bot replies", "chat": "Test chat",
        "settings": "Settings"}
# Tabs folded into others: Payments is part of Customers, the Journey's
# messages are Bot replies' "What the bot says", Templates is in Nudges.
MOVED = {"payments": "customers", "journey": "replies", "templates": "nudges"}
# The tabs with a date range, and the range each starts on.
RANGED = {"overview": "30", "leads": "all", "customers": "all", "nudges": "all"}
NUDGE_SECTIONS = {"rules": "Rules", "flows": "Leads by flow", "sent": "Messages sent",
                  "templates": "WhatsApp templates"}
VIEWS = {"": "All", "attention": "Needs the team", "team": "With the team",
         **{k: v for k, v in c.STAGES.items()}, "test": "Test chat leads"}
ACCESS_VIEWS = {"": "All customers", "opened": "Opened the course", "waiting": "Not confirmed yet",
                "help": "Needs the team", "refunded": "Refunded"}
PAGE_SIZE = 50
BOARD_CARDS = 25
TEST_SOURCE = "Test chat"


def _url(request: Request, tab: str = "overview", **query) -> str:
    params = urlencode({"tab": tab, **{k: v for k, v in query.items() if v}})
    return f"{request.url_for('admin_sft')}?{params}"


def _local(cfg, when) -> str:
    when = leads.aware(when)
    if when is None:
        return ""
    return when.astimezone(ZoneInfo(cfg.timezone)).strftime("%d %b, %I:%M %p")


def _range(request: Request, cfg, tab: str) -> periods.Range:
    q = request.query_params
    return periods.resolve(q.get("range"), q.get("from"), q.get("to"), tz=cfg.timezone,
                           default=RANGED.get(tab, "30"))


async def _count(session, *conditions) -> int:
    return await session.scalar(select(func.count()).select_from(SftLead).where(*conditions)) or 0


@router.get("", name="admin_sft")
async def sft_page(request: Request, tab: str = "overview", page: int | str | None = None,
                   session: AsyncSession = Depends(get_session),
                   current_user: AdminUser = Depends(require_admin)):
    q = request.query_params
    tab = MOVED.get(tab, tab)
    tab = tab if tab in TABS else "overview"
    cfg = await config.load(session, fresh=True)
    rng = _range(request, cfg, tab)
    keep = rng.query() if "range" in q else {}

    def url(t: str, **kw) -> str:
        return _url(request, t, **{**keep, **kw})

    context: dict = {
        "current_user": current_user, "tab": tab, "tabs": TABS, "cfg": cfg, "c": c,
        "modes": MODES, "url": url, "local": lambda when: _local(cfg, when),
        "missing": sft_router.ready(), "zoho_connected": crm.connected(), "rng": rng,
        "presets": periods.PRESETS, "ranged": tab in RANGED,
        "keep": ("&" + urlencode(keep)) if keep else "", "money": cfg.money,
        "attention_count": await _count(session, SftLead.needs_attention.is_(True)),
        "team_count": await _count(session, SftLead.with_team.is_(True)),
        "q": q,
    }
    build = {"overview": _overview, "leads": _leads,
             "customers": _customers, "nudges": _nudges, "offers": _offers,
             "replies": _replies, "chat": _chat, "settings": _settings}.get(tab)
    if build is not None:
        context.update(await build(request, session, cfg, rng, page, url))
    return render(request, "admin/sft/page.html", context)


# --- Overview ----------------------------------------------------------------------
async def _overview(request, session, cfg, rng, page, url) -> dict:
    report = await analytics.report(session, rng, money=cfg.money)
    queue = (await session.execute(
        select(SftLead).where(or_(SftLead.needs_attention.is_(True), SftLead.with_team.is_(True)))
        .order_by(SftLead.attention_priority.desc(), SftLead.attention_since)
        .limit(25))).scalars().all()
    top = max([max(n, p) for _, n, p in report.daily] or [0]) or 1
    return {"report": report, "queue": queue, "chart_top": top}


# --- Leads -------------------------------------------------------------------------
def _lead_filters(rng, view: str, q: str, source: str) -> list:
    where = [SftLead.source == TEST_SOURCE] if view == "test" else [SftLead.source != TEST_SOURCE]
    where += rng.where(SftLead.created_at)
    if view == "attention":
        where.append(SftLead.needs_attention.is_(True))
    elif view == "team":
        where.append(SftLead.with_team.is_(True))
    elif view in c.STAGES:
        where.append(SftLead.stage == view)
    if source:
        where.append(SftLead.source == source)
    if q.strip():
        term = f"%{q.strip()}%"
        where.append(or_(SftLead.name.ilike(term), SftLead.profile_name.ilike(term),
                         SftLead.whatsapp_number.like(term), SftLead.source.ilike(term),
                         SftLead.email.ilike(term)))
    return where


async def _leads(request, session, cfg, rng, page, url) -> dict:
    qp = request.query_params
    view = qp.get("view", "") if qp.get("view", "") in VIEWS else ""
    q, source = qp.get("q", ""), qp.get("source", "")
    layout = "board" if qp.get("layout") == "board" else "list"
    where = _lead_filters(rng, view, q, source)
    sources = (await session.execute(
        select(SftLead.source).where(SftLead.source != TEST_SOURCE).distinct()
        .order_by(SftLead.source))).scalars().all()
    by_stage = dict((await session.execute(
        select(SftLead.stage, func.count()).where(*_lead_filters(rng, "", q, source)
                                                  if view not in c.STAGES else where)
        .group_by(SftLead.stage))).all())
    out = {"view": view, "views": VIEWS, "search": q, "source": source, "sources": sources,
           "layout": layout, "by_stage": by_stage}

    def url_with(**kw):
        return url("leads", view=view, q=q, source=source, layout=layout, **kw)

    out["url_with"] = url_with
    if layout == "board":
        rows = (await session.execute(select(SftLead).where(*where)
                                      .order_by(SftLead.last_activity_at.desc())
                                      .limit(1500))).scalars().all()
        last = await views.last_nudges(session, [r.id for r in rows])
        columns = []
        for stage in (*c.FUNNEL, c.NURTURE, c.LOST):
            cards = [r for r in rows if r.stage == stage]
            columns.append({"stage": stage, "label": c.STAGES[stage],
                            "count": by_stage.get(stage, 0) if not view and len(rows) >= 1500
                            else len(cards),
                            "cards": [(r, views.flow_of(r, cfg, last.get(r.id)))
                                      for r in cards[:BOARD_CARDS]]})
        out["columns"] = columns
        return out
    total = await session.scalar(select(func.count()).select_from(SftLead).where(*where)) or 0
    paged = listing.Page.of(page, PAGE_SIZE, total)
    rows = (await session.execute(select(SftLead).where(*where)
                                  .order_by(SftLead.last_activity_at.desc())
                                  .offset(paged.offset).limit(PAGE_SIZE))).scalars().all()
    last = await views.last_nudges(session, [r.id for r in rows])
    out.update(page=paged, leads=[(r, views.flow_of(r, cfg, last.get(r.id))) for r in rows])
    return out


# --- Customers: the money ----------------------------------------------------------
async def _payment_figures(session, cfg, rng) -> dict:
    """Revenue, link-to-paid, discounts, refunds, revenue by day, who is waiting."""
    base = [SftLead.source != TEST_SOURCE, SftLead.paid_at.is_not(None), *rng.where(SftLead.paid_at)]
    every = (await session.execute(select(SftLead).where(*base))).scalars().all()
    real = [r for r in every if not r.test_payment]
    revenue = sum((Decimal(r.amount_paid or 0) for r in real), Decimal(0))
    fee = Decimal(cfg.course_fee)
    discounts = sum((max(Decimal(0), fee - Decimal(r.amount_paid or 0)) for r in real
                     if r.offer_id), Decimal(0))
    refunds = await _count(session, SftLead.source != TEST_SOURCE, SftLead.refunded_at.is_not(None),
                           *rng.where(SftLead.refunded_at))
    links = await _count(session, SftLead.source != TEST_SOURCE,
                         SftLead.link_sent_at.is_not(None), *rng.where(SftLead.link_sent_at))
    waiting = (await session.execute(
        select(SftLead).where(SftLead.source != TEST_SOURCE, SftLead.stage == c.PAYMENT_PENDING,
                              SftLead.paid_at.is_(None), SftLead.opted_out.is_(False))
        .order_by(SftLead.link_sent_at.desc()).limit(50))).scalars().all()
    days = rng.days(earliest=min((rng.day_of(r.paid_at) for r in every), default=None))
    per_day: dict = {}
    for r in real:
        day = rng.day_of(r.paid_at)
        per_day[day] = per_day.get(day, Decimal(0)) + Decimal(r.amount_paid or 0)
    daily = [(d, per_day.get(d, Decimal(0))) for d in days]
    return {"paid_count": len(real), "revenue": revenue, "discounts": discounts,
            "average": (revenue / len(real)) if real else Decimal(0),
            "test_count": len(every) - len(real), "refunds": refunds, "links": links,
            "waiting": waiting, "daily": daily,
            "daily_top": max([v for _, v in daily] or [Decimal(0)]) or Decimal(1),
            "payment_state": views.payment_state}


async def _offer_names(session, ids) -> dict:
    ids = [i for i in ids if i]
    if not ids:
        return {}
    rows = (await session.execute(select(SftOffer).where(SftOffer.id.in_(ids)))).scalars()
    return {o.id: (o.code or o.title) for o in rows}


# --- Customers: who paid --------------------------------------------------------------
async def _customers(request, session, cfg, rng, page, url) -> dict:
    """Everyone who paid (with their payment, receipt and course access), and
    the money: one page instead of Payments and Customers side by side."""
    out = await _payment_figures(session, cfg, rng)
    out.update(await _students(request, session, cfg, rng, page, url))
    return out


async def _students(request, session, cfg, rng, page, url) -> dict:
    qp = request.query_params
    access = qp.get("access", "") if qp.get("access", "") in ACCESS_VIEWS else ""
    q = qp.get("q", "")
    where = [SftLead.source != TEST_SOURCE, SftLead.paid_at.is_not(None),
             *rng.where(SftLead.paid_at)]
    every = (await session.execute(select(SftLead).where(*where))).scalars().all()
    counts = {"": len(every)}
    for key in ACCESS_VIEWS:
        if key:
            counts[key] = sum(1 for r in every if _access_key(r) == key)
    if access:
        every = [r for r in every if _access_key(r) == access]
    if q.strip():
        term = q.strip().lower()
        every = [r for r in every if term in " ".join(
            str(x or "") for x in (r.name, r.profile_name, r.whatsapp_number, r.email,
                                   r.source)).lower()]
    every.sort(key=lambda r: leads.aware(r.paid_at), reverse=True)
    paged = listing.Page.of(page, PAGE_SIZE, len(every))
    rows = every[paged.offset:paged.offset + PAGE_SIZE]
    return {"students": rows, "page": paged, "access": access, "access_views": ACCESS_VIEWS,
            "access_counts": counts, "search": q, "course_state": views.course_state,
            "receipt_url": lambda lead: f"/sft/receipt/{lead.pay_token}/{receipt.filename(lead)}",
            "url_with": lambda **kw: url("customers", access=access, q=q, **kw),
            "offer_names": await _offer_names(session, {r.offer_id for r in rows})}


def _access_key(lead: SftLead) -> str:
    label, _ = views.course_state(lead)
    return {"Opened the course": "opened", "Needs help": "help", "Team to send access": "help",
            "Refunded": "refunded"}.get(label, "waiting")


# --- Nudges --------------------------------------------------------------------------
async def _nudges(request, session, cfg, rng, page, url) -> dict:
    qp = request.query_params
    section = qp.get("section", "rules")
    if request.query_params.get("tab") == "templates":
        section = "templates"
    section = section if section in NUDGE_SECTIONS else "rules"
    out: dict = {"section": section, "nudge_sections": NUDGE_SECTIONS, "nudge_names": NUDGES,
                 "flows": views.FLOWS, "hours": range(0, 25)}
    if section in ("rules", "templates"):
        out["templates"] = await _template_rows(cfg)
        out["template_status"] = {row["spec"].name: row["status"] for row in out["templates"]}
    if section == "flows":
        chosen = qp.get("flow", "")
        rows = (await session.execute(
            select(SftLead).where(SftLead.source != TEST_SOURCE, *rng.where(SftLead.created_at))
            .order_by(SftLead.last_activity_at.desc()).limit(3000))).scalars().all()
        last = await views.last_nudges(session, [r.id for r in rows])
        states = [(r, views.flow_of(r, cfg, last.get(r.id))) for r in rows]
        counts = {k: 0 for k in views.FLOWS}
        for _, f in states:
            counts[f.key] += 1
        if chosen in views.FLOWS:
            states = [s for s in states if s[1].key == chosen]
        states.sort(key=lambda s: (s[1].at is None, leads.aware(s[1].at) or leads.now()))
        paged = listing.Page.of(page, PAGE_SIZE, len(states))
        out.update(flow=chosen, flow_counts=counts, page=paged,
                   flow_rows=states[paged.offset:paged.offset + PAGE_SIZE],
                   url_with=lambda **kw: url("nudges", section="flows", flow=chosen, **kw))
    if section == "sent":
        where = [SftEvent.kind.in_(("followup", "nudge_skip")), *rng.where(SftEvent.at),
                 SftLead.source != TEST_SOURCE]
        total = await session.scalar(select(func.count()).select_from(SftEvent)
                                     .join(SftLead, SftLead.id == SftEvent.lead_id)
                                     .where(*where)) or 0
        paged = listing.Page.of(page, 100, total)
        sends = (await session.execute(
            select(SftEvent, SftLead).join(SftLead, SftLead.id == SftEvent.lead_id).where(*where)
            .order_by(SftEvent.at.desc()).offset(paged.offset).limit(100))).all()
        by_kind: dict[str, int] = {}
        for data in (await session.execute(
                select(SftEvent.data).join(SftLead, SftLead.id == SftEvent.lead_id)
                .where(SftEvent.kind == "followup", *rng.where(SftEvent.at),
                       SftLead.source != TEST_SOURCE))).scalars():
            name = (data or {}).get("template") or "payment link again"
            by_kind[name] = by_kind.get(name, 0) + 1
        out.update(sends=sends, page=paged, sent_by=by_kind,
                   url_with=lambda **kw: url("nudges", section="sent", **kw))
    return out


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


# --- Offers --------------------------------------------------------------------------
OFFER_STATUSES = {"running": "Running", "scheduled": "Scheduled", "used_up": "Used up",
                  "ended": "Ended", "off": "Off"}


async def _offers(request, session, cfg, rng, page, url) -> dict:
    rows = (await session.execute(select(SftOffer).order_by(SftOffer.created_at.desc())))\
        .scalars().all()
    used = await offers.uses(session)
    stats = {}
    for offer in rows:
        leads_with = (await session.execute(select(SftLead).where(
            SftLead.offer_id == offer.id, SftLead.source != TEST_SOURCE))).scalars().all()
        paid = [r for r in leads_with if r.paid_at and not r.test_payment]
        revenue = sum((Decimal(r.amount_paid or 0) for r in paid), Decimal(0))
        stats[offer.id] = {"applied": len(leads_with), "paid": len(paid), "revenue": revenue,
                           "discount": sum((max(Decimal(0), Decimal(cfg.course_fee)
                                                - Decimal(r.amount_paid or 0)) for r in paid),
                                           Decimal(0)),
                           "status": offers.status(offer, cfg, used),
                           "price": offers.price_after(Decimal(cfg.course_fee), offer),
                           "describe": offers.describe(offer, cfg)}
    totals = {"running": 0, "enrolled": 0, "discount": Decimal(0), "revenue": Decimal(0)}
    counts: dict[str, int] = {}
    for st in stats.values():
        st["status_key"] = st["status"][0].lower().replace(" ", "_")
        counts[st["status_key"]] = counts.get(st["status_key"], 0) + 1
        totals["running"] += int(st["status_key"] == "running")
        totals["enrolled"] += st["paid"]
        totals["discount"] += st["discount"]
        totals["revenue"] += st["revenue"]
    wanted = request.query_params.get("offer", "")
    editing, creating = None, wanted == "new"
    if wanted and not creating:
        try:
            editing = await session.get(SftOffer, uuid.UUID(wanted))
        except ValueError:
            editing = None
        creating = editing is None
    selected = request.query_params.get("status", "")
    shown = rows if selected not in OFFER_STATUSES else \
        [o for o in rows if stats[o.id]["status_key"] == selected]
    source_names = sorted({*cfg.source_codes.values(), "Meta Ad", "WhatsApp"})
    return {"offers": shown, "all_offers": rows, "offer_stats": stats, "editing": editing,
            "creating": creating, "kinds": offers.KINDS, "source_names": source_names,
            "today": offers.today(cfg), "offer_totals": totals, "offer_counts": counts,
            "offer_statuses": OFFER_STATUSES, "selected_status": selected}


# --- Bot replies -------------------------------------------------------------------
async def _replies(request, session, cfg, rng, page, url) -> dict:
    qp = request.query_params
    check_number, check_text = qp.get("number", ""), qp.get("message", "")
    result = None
    if check_text.strip():
        result = await sft_router.explain(session, check_number, check_text, cfg)
    groups: dict[str, list] = {}
    for spec in c.TEXTS.values():
        groups.setdefault(spec.group, []).append(spec)
    sample = {"first": "Priya", "fee": cfg.fee_label(), "amount": cfg.fee_label(),
              "note": "", "when": "in a week", "code": "SFT20",
              "was": cfg.fee_label()}
    return {"text_groups": groups, "sample": sample, "check_number": check_number,
            "check_text": check_text, "check": result, "field_help": c.FIELD_HELP,
            "edited": set(cfg.texts)}


async def _chat(request, session, cfg, rng, page, url) -> dict:
    number = leads.digits(request.query_params.get("number", "")) or "15550000001"
    lead = await leads.get(session, number)
    return {"number": number, "lead": lead,
            "chat": await _timeline(session, lead, kinds=("inbound", "sent", "send_failed",
                                                           "not_sent")) if lead else [],
            "last_buttons": await _last_buttons(session, lead) if lead else []}


async def _settings(request, session, cfg, rng, page, url) -> dict:
    return {"stripe_live": not is_unset(settings.stripe_secret_key),
            "brochure": brochure.describe(cfg), "brochure_max_mb": brochure.MAX_BYTES // 2**20,
            "zoho_statuses": crm.statuses()}


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
    last = await views.last_nudges(session, [lead.id])
    price = await offers.price(session, lead, cfg)
    return render(request, "admin/sft/lead.html", {
        "current_user": current_user, "lead": lead, "cfg": cfg, "c": c,
        "events": await _timeline(session, lead), "drop": drop, "script": script,
        "local": lambda when: _local(cfg, when),
        "enrol_url": flow.enrol_url(lead), "tags": sorted(crm.tags(lead)),
        "lead_status": crm.lead_status(lead),
        "receipt_url": (f"/sft/receipt/{lead.pay_token}/{receipt.filename(lead)}"
                        if lead.paid_at else ""),
        "back": _url(request, "leads"), "flow_state": views.flow_of(lead, cfg, last.get(lead.id)),
        "price": price, "price_text": offers.fee_text(cfg, price),
        "attention_count": await _count(session, SftLead.needs_attention.is_(True)),
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
                await payments.mark_paid(session, lead, cfg, amount=None, currency=cfg.currency)
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
    elif action == "forget":
        return await _forget(request, session, lead, form, who, here)
    else:
        return redirect(here, flash=("error", "Unknown action."))
    lead.crm_dirty = True
    return redirect(here, flash=("success", message))


async def _forget(request, session, lead: SftLead, form, who: str, here: str):
    """Delete the lead, its timeline and its receipt: the bot forgets the
    number, so its next "SFT" starts the journey from the beginning. Zoho
    keeps its record; the ordering bot's own data is not touched."""
    if lead.paid_at and not lead.test_payment and form.get("confirm_paid") != "on":
        return redirect(here, flash=("error", "This lead paid for real: tick the box to confirm "
                                              "you want their payment off the dashboard too."))
    label, token, tail = leads.label(lead), lead.pay_token, lead.whatsapp_number[-4:]
    await session.execute(delete(SftEvent).where(SftEvent.lead_id == lead.id))
    await session.delete(lead)
    await session.flush()
    receipt.forget(token)
    log.info("sft_lead_deleted", by=who, number_tail=tail)
    return redirect(_url(request, "leads"), flash=(
        "success", f"Deleted {label}. If they message SFT again, the bot starts from the "
                   "beginning (their name first). Their Zoho record stays in Zoho."))


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


# --- saving: settings, bot replies, texts, nudges -------------------------------------
async def _save(request, session, data: dict, who: str, back: str, done: str):
    try:
        await config.save(session, data, who)
    except SftConfigError as exc:
        return redirect(back, flash=("error", f"Not saved: {exc}"))
    sending.clear_cache()
    return redirect(back, flash=("success", done))


@router.post("/settings", name="admin_sft_settings")
async def save_settings(request: Request, session: AsyncSession = Depends(get_session),
                        current_user: AdminUser = Depends(require_admin)):
    """The course, the team's numbers and Zoho (Settings page)."""
    form = await request.form()
    cfg = await config.load(session, fresh=True)
    data = cfg.model_dump(mode="json")
    for name in ("brochure_url", "course_url", "course_access_note", "course_fee", "currency",
                 "team_numbers"):
        if name in form:
            data[name] = str(form.get(name) or "").strip()
    for flag in ("zoho_sync", "zoho_tasks"):
        if f"has_{flag}" in form or flag in form:
            data[flag] = form.get(flag) == "on"
    return await _save(request, session, data, current_user.email, _url(request, "settings"),
                       "SFT settings saved.")


@router.post("/brochure", name="admin_sft_brochure")
async def brochure_upload(request: Request, session: AsyncSession = Depends(get_session),
                          current_user: AdminUser = Depends(require_admin)):
    """Replace the brochure PDF the bot sends, or go back to the original."""
    form = await request.form()
    back = _url(request, "settings")
    cfg = await config.load(session, fresh=True)
    data = cfg.model_dump(mode="json")
    old = cfg.brochure_file
    if form.get("action") == "reset":
        data.update(brochure_file="", brochure_label="")
        done = "Back to the original brochure. Leads get it from now on."
    else:
        upload = form.get("brochure")
        if upload is None or not hasattr(upload, "read"):
            return redirect(back, flash=("error", "Choose a PDF file first."))
        raw = await upload.read(brochure.MAX_BYTES + 1)
        try:
            data["brochure_file"] = brochure.store(raw)
        except brochure.BrochureError as exc:
            return redirect(back, flash=("error", str(exc)))
        data["brochure_label"] = (getattr(upload, "filename", "") or "")[:120]
        replaced_link = bool(data.get("brochure_url"))
        data["brochure_url"] = ""
        done = ("New brochure saved: leads get this PDF from now on."
                + (" (It replaces the brochure link that was set.)" if replaced_link else ""))
    try:
        await config.save(session, data, current_user.email)
    except SftConfigError as exc:
        return redirect(back, flash=("error", f"Not saved: {exc}"))
    if old and old != data["brochure_file"]:
        brochure.remove(old)
    return redirect(back, flash=("success", done))


@router.post("/replies", name="admin_sft_replies")
async def save_replies(request: Request, session: AsyncSession = Depends(get_session),
                       current_user: AdminUser = Depends(require_admin)):
    """Who SFT answers and what starts it (Bot replies page)."""
    form = await request.form()
    cfg = await config.load(session, fresh=True)
    data = cfg.model_dump(mode="json")
    # The whitelist and keywords come as one row per input (the dashboard's
    # Bot replies look); a single comma-separated field works too.
    for name in ("test_numbers", "keywords"):
        if name in form:
            data[name] = ", ".join(str(v) for v in form.getlist(name))
    for name in ("mode", "channel_id", "ad_ids", "source_codes"):
        if name in form:
            data[name] = str(form.get(name) or "").strip()
    if "has_ask_name" in form:
        data["ask_name"] = form.get("ask_name") == "on"
    mode = data.get("mode", cfg.mode)
    done = {"off": "Saved. SFT is off: it answers nobody.",
            "test": "Saved. SFT answers only the test numbers.",
            "live": "Saved. SFT is live."}.get(mode, "Saved.")
    return await _save(request, session, data, current_user.email, _url(request, "replies"), done)


@router.post("/texts", name="admin_sft_texts")
async def save_text(request: Request, session: AsyncSession = Depends(get_session),
                    current_user: AdminUser = Depends(require_admin)):
    """One of the bot's messages, edited or put back to the default."""
    form = await request.form()
    key = str(form.get("key") or "")
    back = _url(request, "replies") + f"#text-{key}"
    if key not in c.TEXTS:
        return redirect(back, flash=("error", "Unknown message."))
    cfg = await config.load(session, fresh=True)
    texts = dict(cfg.texts)
    value = str(form.get("text") or "").replace("\r\n", "\n").strip()
    label = c.TEXTS[key].label
    if form.get("action") == "reset" or value == c.TEXTS[key].default.strip():
        texts.pop(key, None)
        done = f"{label}: back to the default wording."
    else:
        problem = c.check_text(key, value)
        if problem:
            return redirect(back, flash=("error", f"{label} not saved: {problem}."))
        texts[key] = value
        done = f"{label}: saved. The bot says it from the next message."
    data = cfg.model_dump(mode="json") | {"texts": texts}
    return await _save(request, session, data, current_user.email, back, done)


def _whole(value, default: int | None = None) -> int | None:
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return int(float(text))
    except ValueError:
        raise SftConfigError(f"{text!r} is not a number") from None


@router.post("/nudges", name="admin_sft_nudges")
async def save_nudges(request: Request, session: AsyncSession = Depends(get_session),
                      current_user: AdminUser = Depends(require_admin)):
    """The Nudges page's rules: on/off, send hours, each nudge's wait and switch,
    and the team's hand-off times."""
    form = await request.form()
    cfg = await config.load(session, fresh=True)
    back = _url(request, "nudges", section="rules")
    data = cfg.model_dump(mode="json")
    try:
        hours = []
        off = []
        for n in range(1, 6):
            wait = _whole(form.get(f"followup_{n}_hours"))
            if wait:
                hours.append(wait)
                if form.get(f"followup_{n}_on") != "on":
                    off.append(f"followup_{len(hours)}")
        for key in ("payment_reminder", "nurture", "course_check"):
            if form.get(f"{key}_on") != "on":
                off.append(key)
        data.update(
            nudges_on=form.get("nudges_on") == "on", nudges_off=off, followup_hours=hours,
            payment_reminder_minutes=_whole(form.get("payment_reminder_minutes"),
                                            cfg.payment_reminder_minutes),
            access_check_hours=_whole(form.get("access_check_hours"), cfg.access_check_hours),
            nurture_days=str(form.get("nurture_days") or "").strip(),
            send_from_hour=_whole(form.get("send_from_hour"), cfg.send_from_hour),
            send_until_hour=_whole(form.get("send_until_hour"), cfg.send_until_hour),
            timezone=str(form.get("timezone") or cfg.timezone).strip())
        attention = dict(cfg.attention_hours)
        for key in attention:
            raw = str(form.get(f"attention_{key}") or "").strip()
            if raw:
                try:
                    attention[key] = max(0.0, float(raw))
                except ValueError:
                    raise SftConfigError(f"team hand-off for {c.DROPS[key].stage_name}: "
                                         f"{raw!r} is not a number") from None
        data["attention_hours"] = attention
    except SftConfigError as exc:
        return redirect(back, flash=("error", f"Not saved: {exc}"))
    if not hours:
        return redirect(back, flash=("error", "Not saved: keep at least one follow-up wait "
                                              "(switch it off if you don't want it sent)."))
    done = "Nudges saved." if data["nudges_on"] else \
        "Nudges saved. They are all off: nothing is sent until you switch them on."
    return await _save(request, session, data, current_user.email, back, done)


@router.post("/templates", name="admin_sft_templates")
async def submit_templates(request: Request, session: AsyncSession = Depends(get_session),
                           current_user: AdminUser = Depends(require_admin)):
    from app.integrations.gallabox import template_admin

    cfg = await config.load(session, fresh=True)
    back = _url(request, "nudges", section="templates")
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


# --- offers -------------------------------------------------------------------------
def _offer_from(form, cfg) -> dict:
    title = str(form.get("title") or "").strip()[:80]
    if not title:
        raise SftConfigError("give the offer a name")
    code = offers.clean_code(form.get("code"))
    if code and not offers.CODE.match(code):
        raise SftConfigError("a code is 3 to 20 letters, numbers or dashes, such as SFT20")
    if code and code.lower() in {k.lower() for k in cfg.keywords}:
        raise SftConfigError(f"{code} is one of SFT's keywords: choose another code")
    kind = str(form.get("kind") or "percent")
    if kind not in offers.KINDS:
        raise SftConfigError("choose a discount type")
    try:
        value = Decimal(str(form.get("value") or "").strip())
    except InvalidOperation:
        raise SftConfigError("the discount is not a number") from None
    fee = Decimal(cfg.course_fee)
    if value <= 0:
        raise SftConfigError("the discount must be more than 0")
    if kind == "percent" and value >= 100:
        raise SftConfigError("a percent off is less than 100")
    if kind == "amount" and value >= fee:
        raise SftConfigError(f"the amount off must be less than the fee ({cfg.money(fee)})")
    if kind == "price" and value >= fee:
        raise SftConfigError(f"the offer price must be less than the fee ({cfg.money(fee)})")
    if kind == "price" and value < offers.MINIMUM:
        raise SftConfigError(f"Stripe can't charge less than {cfg.money(offers.MINIMUM)}")

    def day(name):
        raw = str(form.get(name) or "").strip()
        try:
            return date.fromisoformat(raw) if raw else None
        except ValueError:
            raise SftConfigError(f"{raw!r} is not a date") from None

    starts, ends = day("starts_on"), day("ends_on")
    if starts and ends and ends < starts:
        raise SftConfigError("the offer ends before it starts")
    limit = _whole(form.get("usage_limit"))
    if limit is not None and limit < 1:
        raise SftConfigError("the limit is at least 1 enrollment (or leave it empty)")
    return {"title": title, "code": code, "kind": kind, "value": value.quantize(Decimal("0.01")),
            "starts_on": starts, "ends_on": ends, "usage_limit": limit,
            "sources": [s for s in form.getlist("sources") if s][:20],
            "active": form.get("active") == "on",
            "note": str(form.get("note") or "").strip()[:255] or None}


@router.post("/offers", name="admin_sft_offer_save")
async def save_offer(request: Request, session: AsyncSession = Depends(get_session),
                     current_user: AdminUser = Depends(require_admin)):
    form = await request.form()
    cfg = await config.load(session, fresh=True)
    offer_id = str(form.get("offer_id") or "")
    back = _url(request, "offers", offer=offer_id or "new")
    try:
        values = _offer_from(form, cfg)
    except SftConfigError as exc:
        return redirect(back, flash=("error", f"Offer not saved: {exc}."))
    offer = None
    if offer_id:
        try:
            offer = await session.get(SftOffer, uuid.UUID(offer_id))
        except ValueError:
            offer = None
        if offer is None:
            return redirect(_url(request, "offers"), flash=("error", "That offer is gone."))
    if values["code"]:
        clash = (await session.execute(select(SftOffer).where(
            func.upper(SftOffer.code) == values["code"]))).scalars().all()
        if any(o.id != (offer.id if offer else None) for o in clash):
            return redirect(back, flash=("error", f"Offer not saved: another offer already uses "
                                                  f"the code {values['code']}."))
    if offer is None:
        offer = SftOffer(**values)
        session.add(offer)
        done = f"Offer {values['code'] or values['title']} created."
    else:
        for key, value in values.items():
            setattr(offer, key, value)
        done = f"Offer {values['code'] or values['title']} saved."
    await session.flush()
    log.info("sft_offer_saved", by=current_user.email, code=values["code"])
    return redirect(_url(request, "offers"), flash=("success", done))


@router.post("/offers/{offer_id}/{action}", name="admin_sft_offer_action")
async def offer_action(offer_id: str, action: str, request: Request,
                       session: AsyncSession = Depends(get_session),
                       current_user: AdminUser = Depends(require_admin)):
    back = _url(request, "offers")
    try:
        offer = await session.get(SftOffer, uuid.UUID(offer_id))
    except ValueError:
        offer = None
    if offer is None:
        return redirect(back, flash=("error", "That offer is gone."))
    name = offer.code or offer.title
    if action == "toggle":
        offer.active = not offer.active
        return redirect(back, flash=("success", f"{name} is {'on' if offer.active else 'off'}."))
    if action == "duplicate":
        copy = SftOffer(title=f"{offer.title} (copy)"[:80], code=None, kind=offer.kind,
                        value=offer.value, starts_on=offer.starts_on, ends_on=offer.ends_on,
                        usage_limit=offer.usage_limit, sources=list(offer.sources or []),
                        active=False, note=offer.note)
        if offer.code:
            taken = {str(o or "").upper() for o in (await session.execute(
                select(SftOffer.code))).scalars()}
            for n in range(2, 100):
                if f"{offer.code}{n}".upper() not in taken:
                    copy.code = f"{offer.code}{n}"[:32]
                    break
        session.add(copy)
        await session.flush()
        return redirect(_url(request, "offers", offer=copy.id),
                        flash=("success", f"Copied {name}. It is off until you switch it on."))
    if action == "delete":
        applied = await _count(session, SftLead.offer_id == offer.id)
        if applied:
            return redirect(back, flash=("error", f"{name} is on {applied} lead(s), so it stays "
                                                  "for the numbers: switch it off instead."))
        await session.delete(offer)
        return redirect(back, flash=("success", f"{name} deleted."))
    return redirect(back, flash=("error", "Unknown action."))


# --- Zoho, export -------------------------------------------------------------------
@router.post("/zoho", name="admin_sft_zoho")
async def zoho_actions(request: Request, session: AsyncSession = Depends(get_session),
                       current_user: AdminUser = Depends(require_admin)):
    form = await request.form()
    back = _url(request, "settings")
    try:
        if form.get("action") == "prepare":
            added = await crm.prepare(session)
            message = ("Added to Zoho: " + ", ".join(added)) if added else \
                "Zoho already has every SFT Lead Source and Lead Status."
        else:
            synced = await crm.run(session)
            message = f"Synced {synced} lead(s) to Zoho."
    except (IntegrationError, ConfigurationError) as exc:
        return redirect(back, flash=("error", f"Zoho: {exc}"))
    return redirect(back, flash=("success", message))


@router.get("/export", name="admin_sft_export")
async def export(request: Request, what: str = "leads", session: AsyncSession = Depends(get_session),
                 current_user: AdminUser = Depends(require_admin)):
    """The leads, payments or students in the chosen dates, as a CSV file."""
    cfg = await config.load(session)
    what = what if what in ("leads", "payments", "customers") else "leads"
    rng = _range(request, cfg, "payments" if what == "payments" else what)
    qp = request.query_params
    if what == "leads":
        where = _lead_filters(rng, qp.get("view", ""), qp.get("q", ""), qp.get("source", ""))
        order = SftLead.created_at.desc()
    else:
        where = [SftLead.source != TEST_SOURCE, SftLead.paid_at.is_not(None),
                 *rng.where(SftLead.paid_at)]
        order = SftLead.paid_at.desc()
    rows = (await session.execute(select(SftLead).where(*where).order_by(order)
                                  .limit(20000))).scalars().all()
    names = await _offer_names(session, {r.offer_id for r in rows})
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Name", "WhatsApp", "Email", "Stage", "Goal", "Source", "Source detail",
                     "Came in", "Last move", "Paid at", "Amount", "Currency", "Offer",
                     "Payment", "Course access", "With team", "Needs team", "Zoho id"])
    for r in rows:
        writer.writerow([
            r.name or r.profile_name or "", f"+{r.whatsapp_number}", r.email or "",
            c.STAGES.get(r.stage, r.stage), c.GOALS.get(r.goal or "", ""), r.source,
            r.source_detail or "", _local(cfg, r.created_at), _local(cfg, r.last_activity_at),
            _local(cfg, r.paid_at), str(r.amount_paid or ""), r.currency or "",
            names.get(r.offer_id, r.offer_code or ""),
            views.payment_state(r)[0] if r.paid_at else "",
            views.course_state(r)[0] if r.paid_at else "", "yes" if r.with_team else "",
            "yes" if r.needs_attention else "", r.zoho_id or ""])
    stamp = leads.now().astimezone(ZoneInfo(cfg.timezone)).strftime("%Y-%m-%d")
    return StreamingResponse(
        iter([out.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="sft-{what}-{stamp}.csv"'})
