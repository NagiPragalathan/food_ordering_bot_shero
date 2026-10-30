"""The nudge run: who gets what, now, and sending it.

For each active campaign (highest priority first) the audience finder lists
who qualifies; each customer then passes every rule below, in this order,
and the first that fails is the reason they are skipped:

    opted out (STOP)            -> opted_out
    bot in test mode            -> bot_test_mode
    nudge test numbers set      -> not_a_test_number
    with a human agent          -> with_an_agent
    an audience filter          -> audience:<filter>
    order under way             -> order_in_progress
    nudged for this already     -> already_sent / already_logged / gave_up_after_failures
    campaign per-customer limit -> campaign_limit / campaign_cooldown
    shop-wide frequency caps    -> too_soon / daily_limit / weekly_limit
    local quiet hours and days  -> quiet_hours / not_a_send_day
    the coupon still usable     -> coupon: <why>
    a way to reach them         -> outside_24h_window / us_marketing_blocked /
                                   template_not_approved

A follow-up journey (followup.py) first has to be due - not_due,
journey_paused / _converted / _ended / _finished - and not mid-order or just
chatting (ordering_now, chatted_recently); its message is then picked from
the library (no_message_available, no_approved_template).

The master switch is checked before anything else: off, and a run sends
nothing at all. A preview runs every rule and sends nothing, whatever the
switch says.

Test mode (Settings > Test mode, `test_numbers`): only those numbers are
nudged, they always count for the follow-up journey whatever their orders
(followup.testers), and `send_now` sends one of them a campaign's next
message straight away - no waiting days, quiet hours or frequency limits;
STOP, a hold, an ended journey and WhatsApp's 24-hour rule still apply.

A send is claimed in the database (`nudge_sends.claim_key`, unique) and
committed before the WhatsApp call, so two workers - the scheduler and a
manual run - can never send the same nudge twice.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConfigurationError, IntegrationError
from app.core.logging import get_logger
from app.db.models import (
    ConversationStep,
    Customer,
    FlowStatus,
    JourneyBucket,
    NudgeCampaign,
    NudgeKind,
    NudgeSend,
    NudgeStatus,
    Order,
    Outlet,
    PaymentStatus,
    Promotion,
)
from app.integrations.gallabox import template_status
from app.integrations.gallabox.sender import current_sender
from app.integrations.gallabox.templates import TemplateSpec
from app.integrations.zoho import engagement
from app.integrations.zoho import fields as f
from app.services import allowlist, order_link, promotions
from app.services.customers import get_customer, get_or_create_conversation
from app.services.nudges import audience as aud
from app.services.nudges import followup, render, store
from app.services.nudges.audience import Candidate, Facts, PastOrder, aware, digits
from app.services.nudges.config import (
    BUCKET_LABELS,
    DAYS,
    CampaignConfig,
    GlobalSettings,
    NudgeConfigError,
)

log = get_logger(__name__)

# WhatsApp's 24-hour customer-service window, with a margin for the clock.
SERVICE_WINDOW = timedelta(hours=23, minutes=45)
MAX_FAILURES = 3
ZOHO_ERRORS = (IntegrationError, ConfigurationError)

# One run at a time in this process; the claim covers other processes.
_run_lock = asyncio.Lock()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_us(number: str) -> bool:
    """+1 is the US and Canada (NANP), where Meta holds back marketing templates."""
    return number.startswith("1") and len(number) == 11


# --- the report ------------------------------------------------------------------------
@dataclass
class Outcome:
    campaign_id: str
    campaign: str
    kind: str
    customer_id: str
    whatsapp_number: str
    name: str | None
    status: str          # sent | failed | dry_run | would_send | skipped
    reason: str | None = None
    channel: str | None = None
    message: str | None = None
    detail: str | None = None    # a follow-up's message, month and group

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class CampaignResult:
    id: str
    name: str
    kind: str
    status: str = "ran"          # ran | paused | invalid | not_started | ended
    reason: str | None = None
    candidates: int = 0
    counts: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)
    stopped: str | None = None   # a cap that ended this campaign's turn early

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind, "status": self.status,
                "reason": self.reason, "candidates": self.candidates,
                "sent": self.counts["sent"], "failed": self.counts["failed"],
                "dry_run": self.counts["dry_run"], "would_send": self.counts["would_send"],
                "skipped": dict(self.skipped), "stopped": self.stopped}


@dataclass
class Report:
    mode: str                    # run | preview
    enabled: bool
    dry_run: bool
    started_at: datetime
    stopped: str | None = None   # master_switch_off, run_cap
    campaigns: list[CampaignResult] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)

    @property
    def totals(self) -> dict:
        total = Counter()
        skipped = Counter()
        for result in self.campaigns:
            total.update(result.counts)
            skipped.update(result.skipped)
        return {"sent": total["sent"], "failed": total["failed"], "dry_run": total["dry_run"],
                "would_send": total["would_send"], "skipped": sum(skipped.values()),
                "skipped_by_reason": dict(skipped)}

    def as_dict(self, *, include_skipped: bool = True, limit: int = 200) -> dict:
        outcomes = [o for o in self.outcomes if include_skipped or o.status != "skipped"]
        return {
            "mode": self.mode, "enabled": self.enabled, "dry_run": self.dry_run,
            "started_at": self.started_at.isoformat(), "stopped": self.stopped,
            "totals": self.totals,
            "campaigns": [c.as_dict() for c in self.campaigns],
            "outcomes": [o.as_dict() for o in outcomes[:limit]],
            "outcomes_truncated": max(len(outcomes) - limit, 0),
        }


# --- run state -------------------------------------------------------------------------------
@dataclass
class _State:
    now: datetime
    settings: GlobalSettings
    preview: bool
    dry_run: bool
    actor: str
    inbound: dict[str, datetime]
    outlets: dict[uuid.UUID, Outlet]
    sent_in_run: int = 0
    # Sends made earlier in this run, by customer, for the shop-wide caps.
    run_sends: dict[uuid.UUID, list[datetime]] = field(default_factory=dict)
    # Library messages and menu dishes, read once per run (followup.py).
    library: dict = field(default_factory=dict)
    # A test number's Send now: no waiting, quiet hours or frequency limits.
    fast: bool = False


@dataclass
class _Plan:
    channel: str                  # text | button | template
    text: str                     # what the customer reads (or the template summary)
    url: str | None = None
    button_text: str | None = None
    template: TemplateSpec | None = None
    template_values: tuple[str, ...] = ()
    button_value: str | None = None
    pick: followup.Pick | None = None   # a follow-up's library message


@dataclass
class _History:
    # This campaign's earlier sends to each customer: (dedupe_key, status, sent_at).
    campaign: dict[uuid.UUID, list[tuple[str, str, datetime | None]]] = field(default_factory=dict)
    # Every campaign's real sends in the past week, by customer.
    recent: dict[uuid.UUID, list[datetime]] = field(default_factory=dict)
    sent_today: int = 0


# --- entry points ------------------------------------------------------------------------------
async def run(session: AsyncSession, *, campaign_ids: list[uuid.UUID] | None = None,
              preview: bool = False, actor: str = "scheduler",
              only_customer: Customer | None = None, now: datetime | None = None) -> Report:
    """Run the campaigns (all active ones by default).

    `preview`: evaluate everything, send and record nothing - even with the
    master switch off, and for paused campaigns named in `campaign_ids`.
    `only_customer`: evaluate this one customer (the "why not" view).
    """
    now = now or utcnow()
    enabled, settings = await store.load_settings(session)
    report = Report(mode="preview" if preview else "run", enabled=enabled,
                    dry_run=settings.dry_run, started_at=now)

    if not preview and not enabled:
        report.stopped = "master_switch_off"
        log.info("nudges_skipped_master_switch_off", actor=actor)
        return report

    campaigns = await store.list_campaigns(session)
    if campaign_ids is not None:
        wanted = set(campaign_ids)
        campaigns = [c for c in campaigns if c.id in wanted]

    if preview:
        await _run(session, campaigns, settings, report, now=now, preview=True,
                   actor=actor, only_customer=only_customer,
                   include_paused=campaign_ids is not None or only_customer is not None)
    else:
        async with _run_lock:
            await _run(session, campaigns, settings, report, now=now, preview=False,
                       actor=actor, only_customer=only_customer, include_paused=False)
        totals = report.totals
        if totals["sent"] or totals["failed"] or totals["dry_run"]:
            log.info("nudges_run", actor=actor, **{k: v for k, v in totals.items()
                                                   if k != "skipped_by_reason"})
    return report


async def run_scheduled(session: AsyncSession) -> int:
    """The scheduler's entry point: the number of nudges sent."""
    report = await run(session)
    return report.totals["sent"]


async def _run(session, campaigns, settings, report, *, now, preview, actor,
               only_customer, include_paused) -> None:
    state = _State(now=now, settings=settings, preview=preview,
                   dry_run=settings.dry_run, actor=actor,
                   inbound=await aud.last_inbound(session, now),
                   outlets=await aud.outlets_by_id(session))
    for campaign in campaigns:
        result = CampaignResult(id=str(campaign.id), name=campaign.name, kind=campaign.kind)
        report.campaigns.append(result)
        if not campaign.is_active and not include_paused:
            result.status = "paused"
            continue
        await _run_campaign(session, campaign, state, result, report, only_customer)
        if state.sent_in_run >= settings.max_sends_per_run and not preview:
            report.stopped = "run_cap"
            break


async def _run_campaign(session, campaign: NudgeCampaign, state: _State,
                        result: CampaignResult, report: Report,
                        only_customer: Customer | None) -> None:
    now = state.now
    promotion = await store.promotion_of(session, campaign)
    try:
        config = store.campaign_config(campaign, has_coupon=promotion is not None)
    except NudgeConfigError as exc:
        # Most often: its coupon was deleted and the text still mentions it.
        result.status, result.reason = "invalid", str(exc)
        log.warning("nudge_campaign_invalid", campaign=campaign.name, error=str(exc))
        return
    schedule = config.schedule
    if schedule.starts_at and now < schedule.starts_at:
        result.status = "not_started"
        return
    if schedule.ends_at and now > schedule.ends_at:
        result.status = "ended"
        return

    if campaign.kind == NudgeKind.FOLLOW_UP:
        candidates = await followup.find(session, campaign, config, now, preview=state.preview,
                                         test_numbers=state.settings.test_numbers)
    else:
        candidates = await aud.find(session, campaign.kind, config.trigger, config.audience, now)
    if only_customer is not None:
        candidates = [c for c in candidates if c.customer.id == only_customer.id]
        if not candidates:
            result.skipped["trigger_not_met"] += 1
            report.outcomes.append(_outcome(campaign, None, "skipped", only_customer,
                                            reason="trigger_not_met"))
            return
    result.candidates = len(candidates)
    if not candidates:
        return

    ids = [c.customer.id for c in candidates]
    history = await _history(session, campaign, ids, now)
    busy = await aud.busy_customers(session, ids, now)
    conversations = await aud.conversations_of(session, ids)
    for candidate in candidates:
        candidate.conversation = candidate.conversation or conversations.get(candidate.customer.id)
        if campaign.kind == NudgeKind.TARGETED_OFFER:
            sends = [h for h in history.campaign.get(candidate.customer.id, [])
                     if h[1] == NudgeStatus.SENT]
            candidate.dedupe_key = f"offer#{len(sends) + 1}"

    limits = config.limits
    sent_here = 0
    candidates.sort(key=lambda c: c.since or now)
    for candidate in candidates:
        if not state.preview and state.sent_in_run >= state.settings.max_sends_per_run:
            result.stopped = "run_cap"
            break
        if limits.daily_cap and history.sent_today + sent_here >= limits.daily_cap:
            result.stopped = "campaign_daily_cap"
            break
        if limits.per_run_cap and sent_here >= limits.per_run_cap:
            result.stopped = "campaign_run_cap"
            break

        plan_or_reason = await _evaluate(session, campaign, config, promotion, candidate,
                                         state, history, busy)
        if isinstance(plan_or_reason, str):
            result.skipped[plan_or_reason.split(":")[0]] += 1
            report.outcomes.append(_outcome(campaign, candidate, "skipped",
                                            reason=plan_or_reason))
            if campaign.kind == NudgeKind.FOLLOW_UP and not state.preview:
                # A due follow-up held back: recorded, with the reason.
                await followup.log_skip(session, campaign, candidate, plan_or_reason, now)
            continue
        plan = plan_or_reason

        if state.preview:
            status = "would_send"
        else:
            status = await _deliver(session, campaign, config, promotion, candidate, plan, state)
            if status == "already_sent":
                result.skipped["already_sent"] += 1
                report.outcomes.append(_outcome(campaign, candidate, "skipped",
                                                reason="already_sent"))
                continue
        result.counts[status] += 1
        report.outcomes.append(_outcome(campaign, candidate, status, channel=plan.channel,
                                        message=plan.text,
                                        detail=plan.pick.detail if plan.pick else None))
        if status in ("sent", "would_send", "dry_run"):
            sent_here += 1
            state.sent_in_run += 1
            state.run_sends.setdefault(candidate.customer.id, []).append(now)


def _outcome(campaign, candidate: Candidate | None, status: str, customer: Customer | None = None,
             **extra) -> Outcome:
    customer = customer or candidate.customer
    return Outcome(campaign_id=str(campaign.id), campaign=campaign.name, kind=campaign.kind,
                   customer_id=str(customer.id), whatsapp_number=customer.whatsapp_number,
                   name=customer.name, status=status, **extra)


async def _history(session, campaign: NudgeCampaign, ids: list[uuid.UUID],
                   now: datetime) -> _History:
    history = _History()
    week = now - timedelta(days=7)
    for start in range(0, len(ids), aud.CHUNK):
        chunk = ids[start:start + aud.CHUNK]
        rows = await session.execute(
            select(NudgeSend.customer_id, NudgeSend.campaign_id, NudgeSend.dedupe_key,
                   NudgeSend.status, NudgeSend.sent_at)
            .where(NudgeSend.customer_id.in_(chunk),
                   or_(NudgeSend.campaign_id == campaign.id,
                       and_(NudgeSend.status == NudgeStatus.SENT, NudgeSend.sent_at >= week))))
        for customer_id, campaign_id, key, status, sent_at in rows:
            sent_at = aware(sent_at)
            if campaign_id == campaign.id:
                history.campaign.setdefault(customer_id, []).append((key, status, sent_at))
            if status == NudgeStatus.SENT and sent_at and sent_at >= week:
                history.recent.setdefault(customer_id, []).append(sent_at)
    history.sent_today = (await session.execute(
        select(func.count(NudgeSend.id)).where(
            NudgeSend.campaign_id == campaign.id, NudgeSend.status == NudgeStatus.SENT,
            NudgeSend.sent_at >= now - timedelta(days=1)))).scalar_one()
    return history


# --- the rules ----------------------------------------------------------------------------------
async def _evaluate(session, campaign: NudgeCampaign, config: CampaignConfig,
                    promotion: Promotion | None, candidate: Candidate, state: _State,
                    history: _History, busy: set[uuid.UUID]) -> _Plan | str:
    customer, settings, now = candidate.customer, state.settings, state.now
    number = digits(customer.whatsapp_number)

    if candidate.skip:
        return candidate.skip       # a follow-up not due (followup.find)
    if customer.nudges_opted_out_at is not None:
        return "opted_out"
    if customer.nudge_hold:
        return "on_hold"            # a complaint or support issue the team is handling
    if not number:
        return "no_whatsapp_number"
    if not allowlist.permits(number):
        return "bot_test_mode"
    if settings.test_numbers and number not in settings.test_numbers:
        return "not_a_test_number"
    conversation = candidate.conversation
    if conversation is not None and conversation.step == ConversationStep.HANDED_OVER:
        return "with_an_agent"
    problem = aud.audience_problem(candidate, config.audience, now=now, outlets=state.outlets)
    if problem:
        return f"audience:{problem}"
    if customer.id in busy:
        return "order_in_progress"
    if campaign.kind == NudgeKind.FOLLOW_UP and not state.fast:
        problem = followup.gate(candidate, config.journey, now)
        if problem:
            return problem

    earlier = history.campaign.get(customer.id, [])
    same = [(status, at) for key, status, at in earlier if key == candidate.dedupe_key]
    if any(status in (NudgeStatus.SENT, NudgeStatus.SENDING) for status, _ in same):
        return "already_sent"
    if state.dry_run and any(status == NudgeStatus.DRY_RUN for status, _ in same):
        return "already_logged"
    if sum(status == NudgeStatus.FAILED for status, _ in same) >= MAX_FAILURES:
        return "gave_up_after_failures"

    problem = None if state.fast else _limits_problem(customer, config, settings, history,
                                                      state, now)
    if problem:
        return problem

    kitchen = aud.kitchen_of(candidate, state.outlets)
    zone, candidate.tz_source = _customer_zone(kitchen, settings.timezone)
    candidate.tz = zone.key
    local = now.astimezone(zone)
    window = config.schedule.send_window or settings.send_window
    if not state.fast and not window.allows(local.time()):
        return "quiet_hours"
    if not state.fast and config.schedule.days and DAYS[local.weekday()] not in config.schedule.days:
        return "not_a_send_day"

    if promotion is not None and config.offer.require_eligible:
        try:
            await promotions.check(session, promotion, customer=customer, outlet=kitchen)
        except promotions.PromoError as exc:
            return f"coupon: {exc}"

    return await _plan(session, config, promotion, candidate, kitchen, state, number)


def _limits_problem(customer: Customer, config: CampaignConfig, settings: GlobalSettings,
                    history: _History, state: _State, now: datetime) -> str | None:
    """The campaign's own limits, then the shop-wide frequency caps."""
    earlier = history.campaign.get(customer.id, [])
    sent = sorted(at for _key, status, at in earlier if status == NudgeStatus.SENT and at)
    limits = config.limits
    if limits.max_per_customer is not None and len(sent) >= limits.max_per_customer:
        return "campaign_limit"
    if sent and limits.cooldown_days and sent[-1] > now - timedelta(days=limits.cooldown_days):
        return "campaign_cooldown"

    recent = sorted(history.recent.get(customer.id, []) + state.run_sends.get(customer.id, []))
    if recent and settings.min_hours_between and \
            recent[-1] > now - timedelta(hours=settings.min_hours_between):
        return "too_soon"
    if sum(at > now - timedelta(days=1) for at in recent) >= settings.max_per_customer_per_day:
        return "daily_limit"
    if len(recent) >= settings.max_per_customer_per_week:
        return "weekly_limit"
    return None


def _customer_zone(kitchen, fallback: str) -> tuple[ZoneInfo, str]:
    """Their local time zone: their kitchen's, else the settings' (recorded
    as a fallback, as the handoff asks)."""
    if kitchen is not None and kitchen.timezone:
        try:
            return ZoneInfo(kitchen.timezone), "kitchen"
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return _zone(None, fallback), "fallback"


def _zone(name: str | None, fallback: str) -> ZoneInfo:
    for candidate in (name, fallback, "UTC"):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")


async def _plan(session, config: CampaignConfig, promotion, candidate: Candidate, kitchen,
                state: _State, number: str) -> _Plan | str:
    """How to reach them: a normal message inside the 24-hour window, else
    the approved template, else not at all. A follow-up first picks its
    message from the library."""
    message = config.message
    found = render.values(candidate, settings=state.settings, promotion=promotion,
                          kitchen=kitchen, now=state.now)
    last = state.inbound.get(number)
    window_open = last is not None and last >= state.now - SERVICE_WINDOW

    chosen = None
    if config.journey is not None:
        if not window_open and state.settings.block_marketing_templates_to_us and is_us(number):
            return "us_marketing_blocked"
        chosen = await followup.pick(session, config.journey, candidate, now=state.now,
                                     window_open=window_open, has_coupon=promotion is not None,
                                     cache=state.library, kitchen=kitchen)
        if isinstance(chosen, str):
            return chosen
        message = followup.as_message(chosen.message, config.message)

    if window_open and not message.template.always:
        text = render.fill(message.text, found)
        if message.button == "order_link":
            if not found.get("order_link"):
                return "no_order_link"
            return _Plan("button", text, url=found["order_link"], button_text=message.button_text,
                         pick=chosen)
        if message.button == "url":
            return _Plan("button", text, url=message.button_url, button_text=message.button_text,
                         pick=chosen)
        return _Plan("text", text, pick=chosen)

    template = message.template
    if not template.name:
        return "outside_24h_window"
    if state.settings.block_marketing_templates_to_us and is_us(number):
        return "us_marketing_blocked"
    if not await template_status.is_approved(template.name):
        return "template_not_approved"
    params = tuple(render.fill(p, found) or "-" for p in template.params)
    spec = TemplateSpec(
        name=template.name, category="MARKETING", trigger="Retargeting nudge",
        params=tuple(f"param_{i}" for i in range(1, len(params) + 1)), sample_body="",
        button_kind="dynamic_url" if template.button == "order_link" else "none",
        url_base="order")
    button_value = None
    if template.button == "order_link":
        try:
            button_value = order_link.link_suffix(candidate.customer.id)
        except RuntimeError:
            return "no_order_link"
    summary = f"[template {template.name}] " + " | ".join(params)
    if chosen is not None:
        # The template carries the library text: log what they read.
        summary = f"[template {template.name}]\n{render.fill(message.text, found)}"
    return _Plan("template", summary.strip(), template=spec, template_values=params,
                 button_value=button_value, pick=chosen)


# --- sending ------------------------------------------------------------------------------------
async def _send(number: str, plan: _Plan) -> str | None:
    """Send it; Gallabox's id for the message, when it gives one."""
    sender = current_sender()
    if plan.channel == "template":
        result = await sender.send_template_only(number, plan.template, *plan.template_values,
                                                 button_value=plan.button_value)
    elif plan.channel == "button":
        result = await sender.send_cta_url(number, plan.text, url=plan.url,
                                           display_text=plan.button_text)
    else:
        result = await sender.send_text(number, plan.text)
    return provider_id(result)


def provider_id(result) -> str | None:
    """The message id in Gallabox's answer to a send."""
    if not isinstance(result, dict):
        return None
    for source in (result, result.get("data") or {}, result.get("message") or {}):
        if isinstance(source, dict):
            for name in ("id", "messageId", "message_id", "_id"):
                if source.get(name):
                    return str(source[name])[:120]
    return None


async def _deliver(session, campaign: NudgeCampaign, config: CampaignConfig,
                   promotion: Promotion | None, candidate: Candidate, plan: _Plan,
                   state: _State) -> str:
    customer = candidate.customer
    dry = state.dry_run
    key = f"{'dry:' if dry else ''}{campaign.id}:{customer.id}:{candidate.dedupe_key}"
    row = NudgeSend(
        campaign_id=campaign.id, campaign_name=campaign.name, customer_id=customer.id,
        whatsapp_number=customer.whatsapp_number, kind=campaign.kind,
        dedupe_key=candidate.dedupe_key, claim_key=key[:255],
        status=NudgeStatus.DRY_RUN if dry else NudgeStatus.SENDING,
        channel=plan.channel, message=plan.text,
        promo_code=promotion.code if promotion else None, actor=state.actor,
        sent_at=state.now if dry else None,
        message_code=plan.pick.message.code if plan.pick else None,
        note=_selection_note(plan.pick)[:200] if plan.pick else None)
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        return "already_sent"
    # Committed before sending: another worker now sees the claim.
    await session.commit()
    if dry:
        return "dry_run"

    try:
        row.provider_message_id = await _send(digits(customer.whatsapp_number), plan)
    except Exception as exc:  # noqa: BLE001 - one failed send must not stop the run
        row.status, row.claim_key, row.error = NudgeStatus.FAILED, None, str(exc)[:500]
        if plan.pick is not None:
            await followup.record_failed(session, campaign, config.journey, candidate, state.now)
        await session.commit()
        log.error("nudge_send_failed", campaign=campaign.name,
                  customer_id=str(customer.id), error=str(exc))
        return "failed"

    row.status, row.sent_at = NudgeStatus.SENT, state.now
    if plan.pick is not None:
        journey = await followup.record_sent(session, campaign, config.journey, candidate,
                                             plan.pick, state.now)
        journey.timezone, journey.timezone_source = candidate.tz, candidate.tz_source
        for code, reason in plan.pick.passed or []:
            await followup.log_skip(session, campaign, candidate, reason, state.now,
                                    message_code=code)
    await _prepare_their_order(session, config, promotion, candidate)
    await session.commit()
    log.info("nudge_sent", campaign=campaign.name, kind=campaign.kind,
             customer_id=str(customer.id), channel=plan.channel)

    await _record_in_zoho(customer, campaign, config, plan, row, state, candidate)
    if plan.pick is not None:
        from app.services.nudges import crm_fields

        await crm_fields.sync(session, customer, candidate.journey)
    await session.commit()
    return "sent"


def _selection_note(chosen) -> str:
    """Why this message: the selection reason, and any occasion passed over."""
    note = chosen.reason
    if chosen.passed:
        note += "; passed over " + ", ".join(
            f"{code} ({followup.PASSED_OVER.get(reason, reason).split(': ', 1)[-1]})"
            for code, reason in chosen.passed)
    return note


async def _prepare_their_order(session, config: CampaignConfig, promotion: Promotion | None,
                               candidate: Candidate) -> None:
    """Make the link in the nudge open ready to order: the coupon applied,
    and an unpaid order's dishes back in an empty cart."""
    from app.services import cart as cart_service

    customer = candidate.customer
    wants_coupon = promotion is not None and config.offer.auto_apply
    wants_cart = (candidate.order is not None and config.trigger.restore_cart
                  and candidate.order.items)
    if not (wants_coupon or wants_cart):
        return
    conversation = candidate.conversation or await get_or_create_conversation(session, customer)
    # Never replace a code they chose themselves.
    if wants_coupon and not promotions.stored_code(conversation):
        promotions.remember(conversation, promotion.code)
    if wants_cart and not cart_service.read(conversation):
        cart_service.write(conversation, [
            {"retailer_id": line.get("retailer_id"), "name": line.get("name"),
             "quantity": int(line.get("quantity") or 1),
             "unit_price": str(line.get("unit_price") or "0")}
            for line in candidate.order.items if line.get("retailer_id")])
        # Ours, not theirs: no abandoned-cart reminder for it until they change it.
        conversation.set(cart_restored_at=conversation.get("cart_updated_at"))


async def _record_in_zoho(customer: Customer, campaign: NudgeCampaign, config: CampaignConfig,
                          plan: _Plan, row: NudgeSend, state: _State,
                          candidate: Candidate) -> None:
    """A note and the campaign's tag on their Lead/Contact. Best effort: Zoho
    being down never un-sends a nudge."""
    settings = state.settings
    if not settings.zoho_sync:
        return
    record = zoho_record(customer)
    if record is None:
        row.zoho_error = "no Zoho record for this customer yet"
        return
    module, record_id = record
    noted = False
    try:
        if settings.zoho_notes and config.zoho.note:
            lines = [f"Campaign: {campaign.name} ({campaign.kind})",
                     f"Sent: {state.now:%d %b %Y %H:%M} UTC, WhatsApp {plan.channel}"]
            journey = candidate.journey if plan.pick else None
            if journey is not None:
                group = BUCKET_LABELS[JourneyBucket(plan.pick.bucket)]
                lines += [f"Message: {plan.pick.message.code} - {plan.pick.message.title}",
                          f"Follow-up: month {plan.pick.stage}, {group} group, "
                          f"send {journey.attempts}",
                          f"Selected: {_selection_note(plan.pick)}",
                          f"Their time zone: {candidate.tz} ({candidate.tz_source})",
                          f"Next follow-up: {followup.next_after_send(journey)}"]
            if row.promo_code:
                lines.append(f"Coupon: {row.promo_code}")
            if config.zoho.tag:
                lines.append(f"Tag: {config.zoho.tag}")
            lines += ["", plan.text]
            await engagement.add_note(module, record_id, title=f"WhatsApp nudge: {campaign.name}",
                                      content="\n".join(lines))
            noted = True
        if config.zoho.tag:
            await engagement.add_tags(module, record_id, [config.zoho.tag])
        row.zoho_synced = True
    except ZOHO_ERRORS as exc:
        # A plan without tags (Zoho Free) still has the note, tag named in it.
        if noted and isinstance(exc, IntegrationError) and engagement.unsupported(exc):
            row.zoho_synced = True
            row.zoho_error = "Noted; this Zoho plan has no tags"
            return
        row.zoho_error = str(exc)[:500]
        log.warning("nudge_zoho_sync_failed", campaign=campaign.name,
                    customer_id=str(customer.id), error=str(exc))


def zoho_record(customer: Customer) -> tuple[str, str] | None:
    if customer.zoho_contact_id:
        return f.CONTACTS, customer.zoho_contact_id
    if customer.zoho_lead_id:
        return f.LEADS, customer.zoho_lead_id
    return None


# --- a test send ------------------------------------------------------------------------------
SAMPLE_CART = [{"retailer_id": "sample-1", "name": "Drumstick Sambar", "quantity": 2,
                "unit_price": "12.50"},
               {"retailer_id": "sample-2", "name": "Tomato Rasam", "quantity": 1,
                "unit_price": "9.50"}]


# Why a test did not send, and what to do about it.
HINTS = {
    "outside_24h_window": "WhatsApp only delivers a normal message within 24 hours "
                          "of that phone's last message to the bot. Send the bot any "
                          "message from it, then test again - or set "
                          "message.template for this campaign.",
    "us_marketing_blocked": "Meta does not deliver marketing templates to US numbers. "
                            "Message the bot from the phone first so the normal "
                            "message is used.",
    "template_not_approved": "The template is not approved in Gallabox yet.",
    "no_order_link": "ADMIN_SESSION_SECRET is not set, so no ordering link can be made.",
    "no_message_available": "No active message in the library fits this journey "
                            "(its pool, month and group). Add or switch one on in the "
                            "Messages tab.",
    "no_approved_template": "That phone has not messaged the bot in 24 hours, and none "
                            "of the fitting messages has an approved WhatsApp template. "
                            "Send the bot any message from it, then test again.",
    "opted_out": "This number replied STOP. Reply START OFFERS from it, or press Opt back in.",
    "on_hold": "This number is on hold. Lift the hold to send again.",
    "journey_converted": "This number paid for an order after its journey started, so the "
                         "journey ended - as it should. Press Restart on the journey to test "
                         "again.",
    "journey_ended": "The journey has ended. Press Restart on the journey to test again.",
    "journey_finished": "The journey ran its full length. Press Restart on the journey to "
                        "test again.",
    "journey_paused": "The journey is paused. Press Resume on the journey first.",
    "with_an_agent": "The chat is with one of your team. Close the agent chat first.",
    "order_in_progress": "This number has an order waiting for payment. Pay it or let the "
                         "link expire first.",
    "already_sent": "This one was already sent (the same cart or order is never chased "
                    "twice). Change the cart, or place another order, then try again.",
    "gave_up_after_failures": "Three sends to this number failed. Check the number in "
                              "Gallabox.",
}
# A test number's Send now, when there is nothing for that campaign to chase.
NOTHING_TO_CHASE = {
    NudgeKind.ABANDONED_CART: "Their cart is empty. Add a dish to the cart on WhatsApp or the "
                              "menu page, leave it unpaid, then press Send now.",
    NudgeKind.ABANDONED_CHECKOUT: "No unpaid order whose payment link has expired or failed. "
                                  "Place an order without paying; once its link expires "
                                  "(30 minutes), press Send now.",
}


async def send_now(session: AsyncSession, campaign: NudgeCampaign, customer: Customer, *,
                   actor: str) -> dict:
    """Test mode: a test number's next message from this campaign, now.

    The real send, recorded as one: the journey moves on, Zoho is written,
    a cart or order is never chased twice. Only the waiting is skipped - the
    days until the next follow-up, a cart's or link's wait, an ORDER FOOD's
    days before follow-ups resume, quiet hours, chatting and the frequency
    limits. Works with the master switch off and the campaign paused.
    """
    now = utcnow()
    _, settings = await store.load_settings(session)
    number = digits(customer.whatsapp_number)
    if number not in settings.test_numbers:
        raise NudgeConfigError("Send now is for test numbers only: add this number under "
                               "Settings > Test mode first.")
    promotion = await store.promotion_of(session, campaign)
    config = store.campaign_config(campaign, has_coupon=promotion is not None)
    state = _State(now=now, settings=settings, preview=False, dry_run=settings.dry_run,
                   actor=actor, inbound=await aud.last_inbound(session, now),
                   outlets=await aud.outlets_by_id(session), fast=True)
    notes: list[str] = []

    if campaign.kind == NudgeKind.FOLLOW_UP:
        found = await followup.find(session, campaign, config, now, preview=False,
                                    test_numbers=settings.test_numbers)
    else:
        # As if the wait after the cart or the lapsed link were over.
        wait = timedelta(minutes=config.trigger.after_minutes or 0)
        found = await aud.find(session, campaign.kind, config.trigger, config.audience,
                               now + wait)
    candidate = next((c for c in found if c.customer.id == customer.id), None)
    if candidate is None:
        await session.commit()
        return {"sent": False, "reason": "trigger_not_met",
                "hint": NOTHING_TO_CHASE.get(NudgeKind(campaign.kind), ""), "notes": notes}
    if candidate.skip == "not_due":
        candidate.skip = None
    elif candidate.skip == "in_ordering_flow":
        candidate.journey.flow_status = FlowStatus.ABANDONED
        candidate.skip = None
        notes.append(f"Their ORDER FOOD counted as abandoned, as after "
                     f"{config.journey.resume_after_days:g} days with no order.")

    history = await _history(session, campaign, [customer.id], now)
    busy = await aud.busy_customers(session, [customer.id], now)
    candidate.conversation = candidate.conversation or \
        (await aud.conversations_of(session, [customer.id])).get(customer.id)
    plan = await _evaluate(session, campaign, config, promotion, candidate, state, history, busy)
    if isinstance(plan, str):
        await session.commit()
        return {"sent": False, "reason": plan, "hint": HINTS.get(plan.split(":")[0], ""),
                "notes": notes}
    status = await _deliver(session, campaign, config, promotion, candidate, plan, state)
    if status == "dry_run":
        notes.append("Dry run is on (Settings > Test mode): logged, not sent.")
    log.info("nudge_send_now", campaign=campaign.name, to=number[-4:], status=status,
             actor=actor)
    return {"sent": status == "sent", "status": status,
            "reason": None if status == "sent" else status, "hint": HINTS.get(status, ""),
            "channel": plan.channel, "message": plan.text,
            "detail": plan.pick.detail if plan.pick else None, "notes": notes}


async def send_test(session: AsyncSession, campaign: NudgeCampaign, whatsapp_number: str, *,
                    actor: str, now: datetime | None = None) -> dict:
    """Send this campaign's message to one number, now, to see it on a phone.

    Uses that customer's real cart and orders when the number is a customer,
    sample ones otherwise. Skips every rule except how WhatsApp lets the
    message through (the 24-hour window, or an approved template), and does
    not count toward any limit. Works with the master switch off: it only
    ever reaches the number typed in.
    """
    now = now or utcnow()
    number = digits(whatsapp_number)
    if not 8 <= len(number) <= 15:
        raise NudgeConfigError("whatsapp_number must be a full number with its country code")
    _, settings = await store.load_settings(session)
    promotion = await store.promotion_of(session, campaign)
    config = store.campaign_config(campaign, has_coupon=promotion is not None)

    customer = await get_customer(session, number)
    known = customer is not None
    if customer is None:
        customer = Customer(id=uuid.uuid4(), whatsapp_number=number, name="Test Customer")
    candidate = await _sample_candidate(session, customer, campaign.kind, known=known, now=now,
                                        campaign_id=campaign.id)
    outlets = await aud.outlets_by_id(session)
    state = _State(now=now, settings=settings, preview=False, dry_run=False, actor=actor,
                   inbound=await aud.last_inbound(session, now), outlets=outlets)
    kitchen = aud.kitchen_of(candidate, outlets)
    plan = await _plan(session, config, promotion, candidate, kitchen, state, number)
    if isinstance(plan, str):
        return {"sent": False, "reason": plan, "hint": HINTS.get(plan, "")}

    await _send(number, plan)
    if known:
        # In the log for the record; a test never counts toward a limit.
        session.add(NudgeSend(
            campaign_id=campaign.id, campaign_name=campaign.name, customer_id=customer.id,
            whatsapp_number=number, kind=campaign.kind, dedupe_key="test",
            status=NudgeStatus.TEST, channel=plan.channel, message=plan.text,
            promo_code=promotion.code if promotion else None, actor=actor, sent_at=now,
            message_code=plan.pick.message.code if plan.pick else None))
        await session.flush()
    log.info("nudge_test_sent", campaign=campaign.name, to=number[-4:], actor=actor)
    return {"sent": True, "channel": plan.channel, "message": plan.text,
            "detail": plan.pick.detail if plan.pick else None,
            "used_sample_data": not known}


async def _sample_candidate(session, customer: Customer, kind: str, *, known: bool,
                            now: datetime, campaign_id: uuid.UUID | None = None) -> Candidate:
    facts = (await aud.load_facts(session, [customer.id]))[customer.id] if known else Facts()
    conversation = (await aud.conversations_of(session, [customer.id])).get(customer.id) \
        if known else None
    from app.services import cart as cart_service
    cart = cart_service.read(conversation) if conversation else []
    candidate = Candidate(customer=customer, dedupe_key="test", since=now, facts=facts,
                          conversation=conversation, cart=cart or list(SAMPLE_CART))
    if kind == NudgeKind.FOLLOW_UP and known and campaign_id is not None:
        # Their own place in the journey: its month and group pick the message.
        candidate.journey = (await followup.journeys_for(session, campaign_id,
                                                         [customer.id])).get(customer.id)
    if kind == NudgeKind.ABANDONED_CHECKOUT:
        order = None
        if known:
            order = (await session.execute(
                select(Order).where(Order.customer_id == customer.id,
                                    Order.payment_status != PaymentStatus.PAID)
                .order_by(Order.created_at.desc()).limit(1))).scalar_one_or_none()
        candidate.order = order or Order(order_number="SHO-TEST-0001", total=Decimal("34.50"),
                                         items=list(SAMPLE_CART))
    if facts.last is None and kind in (NudgeKind.REPEAT_PURCHASE, NudgeKind.TARGETED_OFFER):
        candidate.facts = Facts(orders=[PastOrder(
            id=uuid.uuid4(), order_number="SHO-TEST-0000", total=Decimal("34.50"),
            paid_at=now - timedelta(days=14), items=list(SAMPLE_CART), outlet_id=None)])
    return candidate
