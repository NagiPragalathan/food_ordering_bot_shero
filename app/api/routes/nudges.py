"""JSON API for retargeting nudges (services/nudges, docs/nudges.md).

Not linked from the dashboard yet. Every route takes either the admin
dashboard's sign-in cookie or the `X-Ops-Key` header (OPS_API_KEY), so it
works from the dashboard later and from curl or Postman now.

    GET    /api/nudges                         overview: switch, settings, campaigns
    GET    /api/nudges/reference               every option, placeholder and default
    GET    /api/nudges/options                 coupons, kitchens, cuisines, dishes for pickers
    GET    /api/nudges/settings                PATCH to change; "enabled" is the master switch
    POST   /api/nudges/enable | /disable       the master switch
    GET    /api/nudges/campaigns               POST to create
    GET    /api/nudges/campaigns/{id}          PATCH to change, DELETE to remove
    POST   /api/nudges/campaigns/{id}/activate | /pause | /duplicate
    POST   /api/nudges/campaigns/{id}/preview  who would get it now; sends nothing
    POST   /api/nudges/campaigns/{id}/test     send it to one number now
    POST   /api/nudges/campaigns/{id}/run      run this campaign now
    POST   /api/nudges/preview | /run          all active campaigns
    GET    /api/nudges/sends                   the send log
    GET    /api/nudges/stats                   sends, conversions, revenue
    GET    /api/nudges/customers/{number}      one customer: history and "why not"
    POST   /api/nudges/customers/{number}/opt-out | /opt-in

    GET    /api/nudges/messages                the follow-up message library; POST to add
    GET    /api/nudges/messages/{id}           PATCH to change, DELETE to remove
    POST   /api/nudges/messages/{id}/duplicate
    POST   /api/nudges/messages/import         an .xlsx, .csv or .json file: check, then apply
    GET    /api/nudges/dashboard               the SMO dashboard: buckets, flow, Dead Leads, held back
    POST   /api/nudges/customers/{number}/hold     {hold, note}: hold their nudges (complaint...)
    POST   /api/nudges/customers/{number}/release  lift the hold
    GET    /api/nudges/campaigns/export        every campaign as ?format=xlsx|csv|json
    GET    /api/nudges/campaigns/sample        an example file in the import layout
    POST   /api/nudges/campaigns/import        campaigns from a file, matched on name: check, then apply
    GET    /api/nudges/messages/export         ?format=xlsx|csv|json, the library as a file
    GET    /api/nudges/messages/sample         ?format=..., a file to start from
    GET    /api/nudges/journeys/summary        each follow-up campaign: who is where
    POST   /api/nudges/journeys/{id}           move / pause / resume / restart / end / due_now
"""

from __future__ import annotations

import hmac
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import (APIRouter, Body, Depends, File, Form, Header, HTTPException, Query, Request,
                     Response, UploadFile, status)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.auth import SESSION_COOKIE, get_user, read_session
from app.core.config import settings
from app.db.models import (Cuisine, Customer, MenuItem, NudgeCampaign, NudgeMessage, NudgeSend,
                           NudgeStatus, Outlet, Promotion)
from app.db.session import get_session
from app.services import promotions
from app.services.customers import get_customer
from app.services.nudges import audience as aud
from app.services.nudges import config as nudge_config
from app.integrations.gallabox import template_status
from app.services import reply_triggers
from app.services.nudges import (campaign_files, crm_fields, followup, library, optout, reports,
                                 runner, store)
from app.services.nudges.config import NudgeConfigError

router = APIRouter(prefix="/api/nudges", tags=["nudges"])


async def nudge_actor(request: Request, x_ops_key: str | None = Header(default=None),
                      session: AsyncSession = Depends(get_session)) -> str:
    """Who is calling: the ops key, or a signed-in admin (their email)."""
    if x_ops_key is not None:
        if settings.ops_api_key and hmac.compare_digest(x_ops_key, settings.ops_api_key):
            return "ops-key"
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid ops key")
    payload = read_session(request.cookies.get(SESSION_COOKIE))
    if payload:
        user = await get_user(session, payload.get("id", ""))
        if user is not None and user.is_active:
            return user.email
    raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                        "Sign in to the admin dashboard, or send the X-Ops-Key header.")


def _invalid(exc: NudgeConfigError) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))


async def _campaign(session: AsyncSession, campaign_id: str) -> NudgeCampaign:
    campaign = await store.get_campaign(session, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such campaign.")
    return campaign


async def _view(session: AsyncSession, campaign: NudgeCampaign) -> dict:
    # Timestamps are set by the database; read them back after a write.
    await session.refresh(campaign)
    return store.campaign_view(campaign, await store.promotion_of(session, campaign))


def _settings_view(enabled: bool, values) -> dict:
    return {"enabled": enabled, "settings": values.model_dump(mode="json")}


# --- overview, reference, settings ------------------------------------------------------
@router.get("")
async def overview(session: AsyncSession = Depends(get_session),
                   actor: str = Depends(nudge_actor)) -> dict:
    enabled, values = await store.load_settings(session)
    campaigns = await store.list_campaigns(session)
    day = datetime.now(timezone.utc) - timedelta(days=1)
    rows = await session.execute(
        select(NudgeSend.status, func.count(NudgeSend.id))
        .where(NudgeSend.created_at >= day).group_by(NudgeSend.status))
    return {
        **_settings_view(enabled, values),
        "campaigns": [await _view(session, c) for c in campaigns],
        "active_campaigns": sum(c.is_active for c in campaigns),
        "last_24_hours": {str(s): n for s, n in rows},
    }


@router.get("/reference")
async def reference(actor: str = Depends(nudge_actor)) -> dict:
    return {**nudge_config.reference(), "campaign_columns": campaign_files.columns_help()}


@router.get("/options")
async def options(session: AsyncSession = Depends(get_session),
                  actor: str = Depends(nudge_actor)) -> dict:
    """The choices behind the editor's pickers: coupons, kitchens, cuisines,
    dishes and lead sources, from what is in the database now."""
    today = await promotions.local_today(session)
    coupons = list((await session.execute(
        select(Promotion).order_by(Promotion.display_order, Promotion.created_at.desc()))).scalars())
    used = await promotions.usage(session, [p.id for p in coupons])
    kitchens = (await session.execute(select(Outlet).order_by(Outlet.name))).scalars()
    cuisines = {c for c in (await session.execute(select(Cuisine.name))).scalars()}
    cuisines |= {c for c in (await session.execute(
        select(Customer.cuisine_preference).distinct())).scalars() if c}
    sources = {s for s in (await session.execute(
        select(Customer.lead_source).distinct())).scalars() if s}
    dishes = (await session.execute(select(MenuItem.name).distinct().order_by(MenuItem.name))).scalars()
    pools = set((await session.execute(select(NudgeMessage.pool).distinct())).scalars())
    return {
        "pools": sorted(pools | {library.DEFAULT_POOL}, key=str.lower),
        # Every library message asks for ORDER FOOD: it must start the bot.
        "order_food_starts_bot": (reply_triggers.mode() == reply_triggers.ANY
                                  or reply_triggers.matching("ORDER FOOD") is not None),
        "coupons": [{"code": p.code, "title": p.title, "headline": promotions.headline(p),
                     "placement": p.placement,
                     "status": promotions.status(p, today=today, used=used[p.id].used),
                     "ends_on": p.ends_on.isoformat() if p.ends_on else None}
                    for p in coupons],
        "kitchens": [{"code": k.code, "name": k.name, "timezone": k.timezone,
                      "is_active": k.is_active} for k in kitchens],
        "cuisines": sorted(cuisines, key=str.lower),
        "lead_sources": sorted(sources | {"Meta Ad", "WhatsApp"}, key=str.lower),
        "dishes": list(dishes),
    }


@router.get("/settings")
async def get_settings(session: AsyncSession = Depends(get_session),
                       actor: str = Depends(nudge_actor)) -> dict:
    return _settings_view(*await store.load_settings(session))


@router.patch("/settings")
async def patch_settings(body: dict = Body(...), session: AsyncSession = Depends(get_session),
                         actor: str = Depends(nudge_actor)) -> dict:
    try:
        return _settings_view(*await store.save_settings(session, body, actor=actor))
    except NudgeConfigError as exc:
        raise _invalid(exc) from None


@router.post("/enable")
async def enable(session: AsyncSession = Depends(get_session),
                 actor: str = Depends(nudge_actor)) -> dict:
    await store.set_enabled(session, True, actor=actor)
    return {"enabled": True}


@router.post("/disable")
async def disable(session: AsyncSession = Depends(get_session),
                  actor: str = Depends(nudge_actor)) -> dict:
    """Off: the scheduler and manual runs send nothing until it is on again."""
    await store.set_enabled(session, False, actor=actor)
    return {"enabled": False}


# --- campaigns -------------------------------------------------------------------------------
@router.get("/campaigns")
async def list_campaigns(session: AsyncSession = Depends(get_session),
                         actor: str = Depends(nudge_actor)) -> dict:
    return {"campaigns": [await _view(session, c) for c in await store.list_campaigns(session)],
            "columns": campaign_files.columns_help()}


@router.post("/campaigns", status_code=status.HTTP_201_CREATED)
async def create_campaign(body: dict = Body(...), session: AsyncSession = Depends(get_session),
                          actor: str = Depends(nudge_actor)) -> dict:
    try:
        nudge_config.parse_active_kind(body.get("kind"))
        campaign = await store.create_campaign(session, body, actor=actor)
    except NudgeConfigError as exc:
        raise _invalid(exc) from None
    return await _view(session, campaign)


@router.get("/campaigns/export")
async def export_campaigns(format_: str = Query("xlsx", alias="format"),
                           session: AsyncSession = Depends(get_session),
                           actor: str = Depends(nudge_actor)) -> Response:
    views = [await _view(session, c) for c in await store.list_campaigns(session)]
    try:
        content, media_type = campaign_files.export(views, format_)
    except NudgeConfigError as exc:
        raise _invalid(exc) from None
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _download(content, media_type, f"nudge-campaigns-{stamp}.{format_}")


@router.get("/campaigns/sample")
async def sample_campaigns(format_: str = Query("xlsx", alias="format"),
                           actor: str = Depends(nudge_actor)) -> Response:
    try:
        content, media_type = campaign_files.sample(format_)
    except NudgeConfigError as exc:
        raise _invalid(exc) from None
    return _download(content, media_type, f"nudge-campaigns-sample.{format_}")


@router.post("/campaigns/import")
async def import_campaigns(file: UploadFile = File(...), mode: str = Form(campaign_files.UPSERT),
                           commit: bool = Form(False), session: AsyncSession = Depends(get_session),
                           actor: str = Depends(nudge_actor)) -> dict:
    """Check a file of campaigns (commit false: nothing changes) or import it.

    Matched on the name; new campaigns come in paused. Rows with a problem are
    listed with the reason and skipped; the rest go in.
    """
    raw = await file.read(campaign_files.MAX_FILE_BYTES + 1)
    try:
        parsed = campaign_files.read_file(file.filename or "", raw)
        return await campaign_files.import_rows(session, parsed, mode=mode, commit=commit,
                                                actor=actor)
    except NudgeConfigError as exc:
        raise _invalid(exc) from None


@router.get("/campaigns/{campaign_id}")
async def get_campaign(campaign_id: str, session: AsyncSession = Depends(get_session),
                       actor: str = Depends(nudge_actor)) -> dict:
    campaign = await _campaign(session, campaign_id)
    view = await _view(session, campaign)
    view["stats_30_days"] = await reports.stats(session, days=30, campaign_id=campaign.id)
    return view


@router.patch("/campaigns/{campaign_id}")
async def patch_campaign(campaign_id: str, body: dict = Body(...),
                         session: AsyncSession = Depends(get_session),
                         actor: str = Depends(nudge_actor)) -> dict:
    campaign = await _campaign(session, campaign_id)
    try:
        if "kind" in body:
            nudge_config.parse_active_kind(body["kind"])
        await store.update_campaign(session, campaign, body, actor=actor)
    except NudgeConfigError as exc:
        raise _invalid(exc) from None
    return await _view(session, campaign)


@router.delete("/campaigns/{campaign_id}")
async def delete_campaign(campaign_id: str, session: AsyncSession = Depends(get_session),
                          actor: str = Depends(nudge_actor)) -> dict:
    await store.delete_campaign(session, await _campaign(session, campaign_id), actor=actor)
    return {"deleted": True}


@router.post("/campaigns/{campaign_id}/activate")
async def activate(campaign_id: str, session: AsyncSession = Depends(get_session),
                   actor: str = Depends(nudge_actor)) -> dict:
    campaign = await _campaign(session, campaign_id)
    try:
        await store.update_campaign(session, campaign, {"is_active": True}, actor=actor)
    except NudgeConfigError as exc:
        raise _invalid(exc) from None
    return await _view(session, campaign)


@router.post("/campaigns/{campaign_id}/pause")
async def pause(campaign_id: str, session: AsyncSession = Depends(get_session),
                actor: str = Depends(nudge_actor)) -> dict:
    campaign = await _campaign(session, campaign_id)
    campaign.is_active, campaign.updated_by = False, actor
    await session.flush()
    return await _view(session, campaign)


@router.post("/campaigns/{campaign_id}/duplicate", status_code=status.HTTP_201_CREATED)
async def duplicate(campaign_id: str, session: AsyncSession = Depends(get_session),
                    actor: str = Depends(nudge_actor)) -> dict:
    copy = await store.duplicate_campaign(session, await _campaign(session, campaign_id),
                                          actor=actor)
    return await _view(session, copy)


@router.post("/campaigns/{campaign_id}/preview")
async def preview_campaign(campaign_id: str, limit: int = Query(100, ge=1, le=1000),
                           include_skipped: bool = True,
                           session: AsyncSession = Depends(get_session),
                           actor: str = Depends(nudge_actor)) -> dict:
    """Who would get it right now and the exact message - or why not. Sends
    and records nothing; works on a paused campaign and with nudges off."""
    campaign = await _campaign(session, campaign_id)
    report = await runner.run(session, campaign_ids=[campaign.id], preview=True, actor=actor)
    return report.as_dict(include_skipped=include_skipped, limit=limit)


@router.post("/campaigns/{campaign_id}/test")
async def test_campaign(campaign_id: str, body: dict = Body(...),
                        session: AsyncSession = Depends(get_session),
                        actor: str = Depends(nudge_actor)) -> dict:
    campaign = await _campaign(session, campaign_id)
    try:
        return await runner.send_test(session, campaign, str(body.get("whatsapp_number") or ""),
                                      actor=actor)
    except NudgeConfigError as exc:
        raise _invalid(exc) from None


@router.post("/campaigns/{campaign_id}/run")
async def run_campaign(campaign_id: str, limit: int = Query(100, ge=1, le=1000),
                       session: AsyncSession = Depends(get_session),
                       actor: str = Depends(nudge_actor)) -> dict:
    campaign = await _campaign(session, campaign_id)
    if not campaign.is_active:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "The campaign is paused. Activate it first, or use preview.")
    return await _run(session, [campaign.id], actor, limit)


@router.post("/preview")
async def preview_all(limit: int = Query(100, ge=1, le=1000), include_skipped: bool = True,
                      session: AsyncSession = Depends(get_session),
                      actor: str = Depends(nudge_actor)) -> dict:
    report = await runner.run(session, preview=True, actor=actor)
    return report.as_dict(include_skipped=include_skipped, limit=limit)


@router.post("/run")
async def run_all(limit: int = Query(100, ge=1, le=1000),
                  session: AsyncSession = Depends(get_session),
                  actor: str = Depends(nudge_actor)) -> dict:
    return await _run(session, None, actor, limit)


async def _run(session, campaign_ids, actor: str, limit: int) -> dict:
    report = await runner.run(session, campaign_ids=campaign_ids, actor=actor)
    if report.stopped == "master_switch_off":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Nudges are switched off, so nothing was sent. "
                            "POST /api/nudges/enable to turn them on.")
    return report.as_dict(include_skipped=False, limit=limit)


# --- log and stats ----------------------------------------------------------------------------
def _uuid(raw: str | None, name: str) -> uuid.UUID | None:
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{name} is not an id") from None


@router.get("/sends")
async def sends(campaign_id: str | None = None, customer_id: str | None = None,
                status_: str | None = Query(None, alias="status"),
                days: int | None = Query(None, ge=1, le=3650),
                limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                session: AsyncSession = Depends(get_session),
                actor: str = Depends(nudge_actor)) -> dict:
    if status_ and status_ not in {str(s) for s in NudgeStatus}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"status is one of {[str(s) for s in NudgeStatus]}")
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    return await reports.list_sends(
        session, campaign_id=_uuid(campaign_id, "campaign_id"),
        customer_id=_uuid(customer_id, "customer_id"), status=status_, since=since,
        limit=limit, offset=offset)


@router.get("/stats")
async def stats(days: int = Query(30, ge=1, le=3650), campaign_id: str | None = None,
                session: AsyncSession = Depends(get_session),
                actor: str = Depends(nudge_actor)) -> dict:
    return await reports.stats(session, days=days, campaign_id=_uuid(campaign_id, "campaign_id"))


@router.get("/dashboard")
async def dashboard(days: int = Query(30, ge=1, le=3650),
                    session: AsyncSession = Depends(get_session),
                    actor: str = Depends(nudge_actor)) -> dict:
    """The SMO dashboard (handoff section 11): journeys by bucket and month,
    the ordering flow, Dead Leads, what was held back and why, suppressions
    and failures. Per-message results are in /stats."""
    return await reports.smo_dashboard(session, days=days)


# --- one customer -----------------------------------------------------------------------------
async def _customer(session: AsyncSession, number: str):
    customer = await get_customer(session, aud.digits(number))
    if customer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No customer with that WhatsApp number.")
    return customer


@router.get("/customers/{number}")
async def customer_detail(number: str, session: AsyncSession = Depends(get_session),
                          actor: str = Depends(nudge_actor)) -> dict:
    """Their nudge history, and for every campaign whether they would get it
    now - and if not, why not."""
    customer = await _customer(session, number)
    now = datetime.now(timezone.utc)
    _, nudge_settings = await store.load_settings(session)
    facts = (await aud.load_facts(session, [customer.id]))[customer.id]
    last = (await aud.last_inbound(session, now)).get(aud.digits(customer.whatsapp_number))
    report = await runner.run(session, preview=True, actor=actor, only_customer=customer,
                              now=now)
    active = {str(c.id): c.is_active for c in await store.list_campaigns(session)}
    return {
        "customer_id": str(customer.id),
        "whatsapp_number": customer.whatsapp_number,
        "name": customer.name,
        # Settings > Test mode: this number gets Send now on each campaign.
        "test_number": aud.digits(customer.whatsapp_number) in nudge_settings.test_numbers,
        "opted_out_at": customer.nudges_opted_out_at.isoformat()
        if customer.nudges_opted_out_at else None,
        "hold": customer.nudge_hold,
        "hold_label": crm_fields.NUDGE_HOLDS.get(customer.nudge_hold or "", None),
        "hold_note": customer.nudge_hold_note,
        "hold_at": customer.nudge_hold_at.isoformat() if customer.nudge_hold_at else None,
        "hold_by": customer.nudge_hold_by,
        "holds": crm_fields.NUDGE_HOLDS,
        "paid_orders": facts.paid_orders,
        "total_spent": f"{facts.total_spent:.2f}",
        "last_order_at": facts.last_paid_at.isoformat() if facts.last_paid_at else None,
        "last_message_at": last.isoformat() if last else None,
        "whatsapp_window_open": bool(last and last >= now - runner.SERVICE_WINDOW),
        "campaigns": [{**o.as_dict(), "campaign_active": active.get(o.campaign_id, False)}
                      for o in report.outcomes],
        "history": (await reports.list_sends(session, customer_id=customer.id,
                                             limit=50))["sends"],
        "journeys": await followup.customer_journeys(session, customer),
    }


@router.post("/customers/{number}/send-now")
async def customer_send_now(number: str, body: dict = Body(...),
                            session: AsyncSession = Depends(get_session),
                            actor: str = Depends(nudge_actor)) -> dict:
    """Test mode: send a test number this campaign's next message now,
    without the waiting (runner.send_now). {"campaign_id": "..."}"""
    customer = await _customer(session, number)
    campaign = await _campaign(session, str(body.get("campaign_id") or ""))
    try:
        return await runner.send_now(session, campaign, customer, actor=actor)
    except NudgeConfigError as exc:
        raise _invalid(exc) from None


@router.post("/customers/{number}/opt-out")
async def customer_opt_out(number: str, session: AsyncSession = Depends(get_session),
                           actor: str = Depends(nudge_actor)) -> dict:
    customer = await _customer(session, number)
    changed = await optout.opt_out(session, customer, source=f"api:{actor}")
    return {"opted_out": True, "changed": changed}


@router.post("/customers/{number}/hold")
async def customer_hold(number: str, body: dict = Body(...),
                        session: AsyncSession = Depends(get_session),
                        actor: str = Depends(nudge_actor)) -> dict:
    """Hold all nudges to them - a complaint or support issue being handled,
    or a number that cannot be reached - until the hold is lifted."""
    customer = await _customer(session, number)
    try:
        await optout.hold(session, customer, str(body.get("hold") or ""),
                          str(body.get("note") or ""), actor=actor)
    except NudgeConfigError as exc:
        raise _invalid(exc) from None
    return {"hold": customer.nudge_hold}


@router.post("/customers/{number}/release")
async def customer_release(number: str, session: AsyncSession = Depends(get_session),
                           actor: str = Depends(nudge_actor)) -> dict:
    customer = await _customer(session, number)
    return {"hold": None, "changed": await optout.release(session, customer, actor=actor)}


@router.post("/customers/{number}/opt-in")
async def customer_opt_in(number: str, session: AsyncSession = Depends(get_session),
                          actor: str = Depends(nudge_actor)) -> dict:
    customer = await _customer(session, number)
    changed = await optout.opt_in(session, customer, source=f"api:{actor}")
    return {"opted_out": False, "changed": changed}


# --- the follow-up message library -------------------------------------------------------
async def _message(session: AsyncSession, message_id: str):
    message = await library.get_message(session, message_id)
    if message is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such message.")
    return message


async def _message_view(session: AsyncSession, message) -> dict:
    await session.refresh(message)
    view = library.view(message)
    view["wa_approved"] = (await template_status.is_approved(message.wa_template)
                           if message.wa_template else None)
    return view


@router.get("/messages")
async def list_messages(pool: str | None = None, days: int = Query(90, ge=1, le=3650),
                        session: AsyncSession = Depends(get_session),
                        actor: str = Depends(nudge_actor)) -> dict:
    """The library, with each message's results and whether its template is approved."""
    messages = await library.list_messages(session)
    stats = {m["code"]: m for m in (await reports.stats(session, days=days))["messages"]}
    approved = {name: await template_status.is_approved(name)
                for name in {m.wa_template for m in messages if m.wa_template}}
    return {
        "messages": [{**library.view(m), "stats": stats.get(m.code),
                      "wa_approved": approved.get(m.wa_template)}
                     for m in messages if not pool or m.pool == pool],
        "pools": sorted({m.pool for m in messages} | {library.DEFAULT_POOL}, key=str.lower),
        "stats_days": days,
        "columns": [{"field": name, "heading": heading, "help": library.HELP[name]}
                    for name, (heading, _) in library.COLUMNS.items()],
    }


@router.post("/messages", status_code=status.HTTP_201_CREATED)
async def create_message(body: dict = Body(...), session: AsyncSession = Depends(get_session),
                         actor: str = Depends(nudge_actor)) -> dict:
    try:
        message = await library.create_message(session, body, actor=actor)
    except NudgeConfigError as exc:
        raise _invalid(exc) from None
    return await _message_view(session, message)


def _download(content: bytes, media_type: str, name: str) -> Response:
    return Response(content=content, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/messages/export")
async def export_messages(format_: str = Query("xlsx", alias="format"), pool: str | None = None,
                          session: AsyncSession = Depends(get_session),
                          actor: str = Depends(nudge_actor)) -> Response:
    messages = await library.list_messages(session, pool=pool or None)
    try:
        content, media_type = library.export(messages, format_)
    except NudgeConfigError as exc:
        raise _invalid(exc) from None
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _download(content, media_type, f"nudge-messages-{stamp}.{format_}")


@router.get("/messages/sample")
async def sample_messages(format_: str = Query("xlsx", alias="format"),
                          actor: str = Depends(nudge_actor)) -> Response:
    try:
        content, media_type = library.sample(format_)
    except NudgeConfigError as exc:
        raise _invalid(exc) from None
    return _download(content, media_type, f"nudge-messages-sample.{format_}")


@router.post("/messages/import")
async def import_messages(file: UploadFile = File(...), mode: str = Form(library.UPSERT),
                          turn_off_missing: bool = Form(False), commit: bool = Form(False),
                          session: AsyncSession = Depends(get_session),
                          actor: str = Depends(nudge_actor)) -> dict:
    """Check a file (commit false: nothing changes) or import it (commit true).

    Rows with a problem are listed with the reason and skipped; the rest go in.
    """
    raw = await file.read(library.MAX_FILE_BYTES + 1)
    try:
        parsed = library.read_file(file.filename or "", raw)
        return await library.import_rows(session, parsed, mode=mode,
                                         turn_off_missing=turn_off_missing, commit=commit,
                                         actor=actor)
    except NudgeConfigError as exc:
        raise _invalid(exc) from None


@router.get("/messages/{message_id}")
async def get_message(message_id: str, session: AsyncSession = Depends(get_session),
                      actor: str = Depends(nudge_actor)) -> dict:
    return await _message_view(session, await _message(session, message_id))


@router.patch("/messages/{message_id}")
async def patch_message(message_id: str, body: dict = Body(...),
                        session: AsyncSession = Depends(get_session),
                        actor: str = Depends(nudge_actor)) -> dict:
    message = await _message(session, message_id)
    try:
        await library.update_message(session, message, body, actor=actor)
    except NudgeConfigError as exc:
        raise _invalid(exc) from None
    return await _message_view(session, message)


@router.delete("/messages/{message_id}")
async def delete_message(message_id: str, session: AsyncSession = Depends(get_session),
                         actor: str = Depends(nudge_actor)) -> dict:
    await library.delete_message(session, await _message(session, message_id), actor=actor)
    return {"deleted": True}


@router.post("/messages/{message_id}/duplicate", status_code=status.HTTP_201_CREATED)
async def duplicate_message(message_id: str, session: AsyncSession = Depends(get_session),
                            actor: str = Depends(nudge_actor)) -> dict:
    copy = await library.duplicate_message(session, await _message(session, message_id),
                                           actor=actor)
    return await _message_view(session, copy)


# --- follow-up journeys ----------------------------------------------------------------------
@router.get("/journeys/summary")
async def journeys_summary(session: AsyncSession = Depends(get_session),
                           actor: str = Depends(nudge_actor)) -> dict:
    return {"campaigns": await followup.summary(session)}


@router.post("/journeys/{journey_id}")
async def change_journey(journey_id: str, body: dict = Body(...),
                         session: AsyncSession = Depends(get_session),
                         actor: str = Depends(nudge_actor)) -> dict:
    """{"action": "move", "bucket": "later"}, or pause / resume / restart / end / due_now."""
    journey = await followup.get_journey(session, journey_id)
    if journey is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such journey.")
    try:
        await followup.act(session, journey, str(body.get("action") or ""),
                           bucket=body.get("bucket"), actor=actor)
    except NudgeConfigError as exc:
        raise _invalid(exc) from None
    await session.refresh(journey)
    campaign = await session.get(NudgeCampaign, journey.campaign_id)
    return followup.journey_view(journey, campaign, datetime.now(timezone.utc))
