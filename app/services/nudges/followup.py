"""Follow-up journeys: months of messages to people who wrote in but never ordered.

A follow-up campaign (kind follow_up) takes everyone who first wrote at least
N days ago and has not paid for an order, and writes to them again and again,
each time with a message from the library (library.py):

    the stage    month 1's messages for the first 30 days, then month 2's...
    the pick     at random from those that fit (or in ID order), never one of
                 their last 9; an occasion message (one with dates) first
                 while it runs
    the timing   by their reply group, each with its own steps:
                   No response  every 3 days x3, every 10 days x3, then 30
                   Later        every 10 days x3, then every 30
                   No           every 30 days, at most 6
    the end      a paid order (converted), the journey's length (183 days
                 from the first send), or the group's last send

Replying LATER or NO (the words are in the nudge settings) moves them to that
group, and its wait starts from the reply. ORDER FOOD starts the ordering
flow (flow_started): the follow-ups wait for the order, and if none comes
within `resume_after_days` the flow counts as abandoned and they resume -
never straight away. A paid order ends the journey.

Following the SMO developer handoff (docs/Shero_SMO_Only_Developer_Handoff.pdf):
the pick never repeats one of their last N messages, nor the angle of their
last few; an occasion message must be inside its dates, before its ordering
cutoff, in its service area and with a dish on the menu, and one passed over
is logged with the reason (nudge_skips). The journey's state is mirrored on
their Zoho record as the handoff's retargeting fields (crm_fields.py).

Each customer's place lives in `nudge_journeys`, written when a send goes out
and never by a preview. Everything else a nudge must pass - STOP, the
shop-wide caps, quiet hours, the 24-hour window, one send per attempt - is
runner.py's, the same as for any campaign.
"""

from __future__ import annotations

import random
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConfigurationError, IntegrationError
from app.core.logging import get_logger
from app.db.models import (
    ConversationStep,
    Customer,
    FlowStatus,
    InboundMessage,
    JourneyBucket,
    JourneyStatus,
    MenuItem,
    NudgeCampaign,
    NudgeJourney,
    NudgeKind,
    NudgeMessage,
    NudgeSend,
    NudgeSkip,
    NudgeStatus,
    Order,
    PaymentStatus,
)
from app.integrations.gallabox import template_status
from app.integrations.zoho import engagement
from app.services.customers import get_customer
from app.services.nudges import audience as aud
from app.services.nudges import library, store
from app.services.nudges.audience import Candidate, aware, digits
from app.services.nudges.config import (
    BUCKET_LABELS,
    COUPON_PLACEHOLDERS,
    CampaignConfig,
    Journey,
    Message,
    NudgeConfigError,
    TemplateMessage,
    placeholders_in,
)

log = get_logger(__name__)

# Messages remembered per journey, for "not one of their last N".
RECENT_KEPT = 30
# Three failed sends of one attempt count as the attempt, so a number that
# cannot be reached does not hold the journey still for ever.
FAILURES_PER_ATTEMPT = 3
# Part-way through an order: the flow is running and they were active today.
ACTIVE_HOURS = 24
IDLE_STEPS = {ConversationStep.START, ConversationStep.COMPLETED}

END_REASONS = {
    "ordered": "Paid for an order",
    "length_reached": "Ran its full length",
    "attempt_limit": "Had the last send of its group",
    "declined": "Replied No (counted as STOP)",
    "ended_by_team": "Ended by your team",
}
# Ends that make them a Dead Lead for this campaign (handoff section 7).
DEAD_REASONS = {"length_reached", "attempt_limit", "declined"}
# The handoff's names for the reply groups and the journey's state, as they
# go to Zoho (Retargeting_Bucket, Retargeting_Status, SMO_Flow_Status).
CRM_BUCKETS = {JourneyBucket.NO_RESPONSE: "No-response", JourneyBucket.LATER: "Later",
               JourneyBucket.NO: "No"}
FLOW_LABELS = {FlowStatus.NOT_STARTED: "Not started", FlowStatus.ACTIVE: "Active",
               FlowStatus.COMPLETED: "Completed", FlowStatus.ABANDONED: "Abandoned"}
# Held back, but tried again on the next run: a deferral, not a skip.
DEFERRALS = {"quiet_hours", "not_a_send_day", "chatted_recently", "ordering_now",
             "order_in_progress", "in_ordering_flow", "too_soon", "daily_limit",
             "weekly_limit", "with_an_agent", "run_cap"}
# Reasons that are not a due follow-up being held back at all.
NOT_DUE = {"not_due", "journey_paused", "journey_converted", "journey_ended",
           "journey_finished", "trigger_not_met", "already_sent", "already_logged",
           "bot_test_mode", "not_a_test_number"}
PASSED_OVER = {
    "occasion_cutoff_passed": "Occasion: its ordering cutoff has passed",
    "occasion_not_in_area": "Occasion: not for their kitchen's area",
    "occasion_dish_unavailable": "Occasion: none of its dishes is on the menu",
}


def crm_bucket(journey: NudgeJourney) -> str:
    """No-response / Later / No / Engaged / Dead Lead."""
    if journey.status == JourneyStatus.ENDED and journey.end_reason in DEAD_REASONS:
        return "Dead Lead"
    if journey.status == JourneyStatus.CONVERTED or journey.flow_status == FlowStatus.ACTIVE:
        return "Engaged"
    return CRM_BUCKETS.get(journey.bucket, journey.bucket)


def crm_status(journey: NudgeJourney | None, customer: Customer) -> str:
    """Active / Paused / Suppressed / Completed."""
    if customer.nudges_opted_out_at is not None or customer.nudge_hold:
        return "Suppressed"
    if journey is None:
        return "Active"
    if journey.status in (JourneyStatus.CONVERTED, JourneyStatus.ENDED):
        return "Completed"
    if journey.status == JourneyStatus.PAUSED or journey.flow_status == FlowStatus.ACTIVE:
        return "Paused"
    return "Active"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def key(round_: int, attempt: int) -> str:
    """What one attempt is about, for nudge_sends.dedupe_key: never sent twice."""
    return f"fu:r{round_}:{attempt}"


def ends_at(journey: NudgeJourney, cfg: Journey) -> datetime:
    return aware(journey.started_at) + timedelta(days=cfg.length_days)


def stage_of(journey: NudgeJourney | None, cfg: Journey, now: datetime) -> int:
    """Which month of the journey it is: 1 until the first send and for 30 days after."""
    if journey is None:
        return 1
    days = (now - aware(journey.started_at)).total_seconds() / 86400
    return min(cfg.stages, int(max(days, 0) // cfg.stage_days) + 1)


def journey_config(campaign: NudgeCampaign | None) -> Journey:
    """The campaign's journey settings; the defaults if it cannot be read."""
    if campaign is None:
        return Journey()
    try:
        return store.campaign_config(campaign, has_coupon=True).journey or Journey()
    except NudgeConfigError:
        return Journey()


# --- who is due ---------------------------------------------------------------------------
async def find(session: AsyncSession, campaign: NudgeCampaign, config: CampaignConfig,
               now: datetime, *, preview: bool) -> list[Candidate]:
    """Everyone the campaign covers, each with their journey and the attempt due.

    Those not due (waiting, paused, finished) come back with `skip` set, so a
    preview and the customer lookup can say why; the runner skips them first.
    """
    cfg = config.journey or Journey()
    candidates = await aud.find(session, NudgeKind.FOLLOW_UP, config.trigger, config.audience, now)
    journeys = await journeys_for(session, campaign.id, [c.customer.id for c in candidates])
    chats = await last_messages(session, now - timedelta(hours=max(cfg.pause_after_chat_hours,
                                                                   ACTIVE_HOURS)))
    finished, abandoned = [], []
    for candidate in candidates:
        candidate.last_inbound = chats.get(digits(candidate.customer.whatsapp_number))
        journey = candidate.journey = journeys.get(candidate.customer.id)
        if journey is None:
            candidate.dedupe_key = key(1, 1)
            continue
        candidate.dedupe_key = key(journey.round, journey.attempts + 1)
        if journey.status != JourneyStatus.ACTIVE:
            candidate.skip = f"journey_{journey.status}"
        elif now >= ends_at(journey, cfg):
            candidate.skip = "journey_finished"
            if not preview:
                end(journey, "length_reached", now)
                finished.append((candidate.customer, journey))
        elif journey.flow_status == FlowStatus.ACTIVE and not flow_lapsed(journey, cfg, now):
            candidate.skip = "in_ordering_flow"
        else:
            if journey.flow_status == FlowStatus.ACTIVE and not preview:
                journey.flow_status = FlowStatus.ABANDONED
                abandoned.append((candidate.customer, journey))
            if aware(journey.next_due_at) > now:
                candidate.skip = "not_due"

    if not preview:
        converted = await _close_converted(session, campaign, now)
        await session.flush()
        from app.services.nudges import crm_fields

        for customer, journey in finished:
            await note_in_zoho(session, customer, f"Follow-up ended: {campaign.name}",
                               f"Dead Lead: {END_REASONS['length_reached'].lower()} "
                               f"({cfg.length_days} days).")
            await crm_fields.sync(session, customer, journey)
        for customer, journey in abandoned:
            await note_in_zoho(session, customer, "Follow-up: ordering flow abandoned",
                               f"No order {cfg.resume_after_days:g} days after "
                               f"{journey.last_keyword or 'ORDER FOOD'}; follow-ups resume "
                               f"({CRM_BUCKETS.get(journey.bucket, journey.bucket)} group, "
                               f"next {next_after_send(journey)}).")
            await crm_fields.sync(session, customer, journey)
        for journey in converted:
            customer = await session.get(Customer, journey.customer_id)
            if customer is not None:
                await crm_fields.sync(session, customer, journey)
    return candidates


def flow_lapsed(journey: NudgeJourney, cfg: Journey, now: datetime) -> bool:
    """ORDER FOOD long enough ago, with no order since, that the flow is abandoned."""
    started = aware(journey.flow_started_at)
    return started is None or now >= started + timedelta(days=cfg.resume_after_days)


async def journeys_for(session: AsyncSession, campaign_id, customer_ids: list) -> dict:
    found = {}
    for start in range(0, len(customer_ids), aud.CHUNK):
        rows = await session.execute(select(NudgeJourney).where(
            NudgeJourney.campaign_id == campaign_id,
            NudgeJourney.customer_id.in_(customer_ids[start:start + aud.CHUNK])))
        found.update({j.customer_id: j for j in rows.scalars()})
    return found


async def last_messages(session: AsyncSession, since: datetime) -> dict[str, datetime]:
    """Each number's last message to the bot since `since`."""
    rows = await session.execute(
        select(InboundMessage.whatsapp_number, func.max(InboundMessage.created_at))
        .where(InboundMessage.created_at >= since).group_by(InboundMessage.whatsapp_number))
    latest: dict[str, datetime] = {}
    for number, at in rows:
        number, at = digits(number), aware(at)
        if number and (number not in latest or at > latest[number]):
            latest[number] = at
    return latest


async def _close_converted(session: AsyncSession, campaign: NudgeCampaign,
                           now: datetime) -> list[NudgeJourney]:
    """A paid order ends the journey: its job is done."""
    paid = exists().where(Order.customer_id == NudgeJourney.customer_id,
                          Order.payment_status.in_([PaymentStatus.PAID, PaymentStatus.REFUNDED]))
    rows = await session.execute(select(NudgeJourney).where(
        NudgeJourney.campaign_id == campaign.id,
        NudgeJourney.status.in_([JourneyStatus.ACTIVE, JourneyStatus.PAUSED]), paid))
    closed = []
    for journey in rows.scalars():
        journey.status, journey.end_reason, journey.ended_at = (
            JourneyStatus.CONVERTED, "ordered", now)
        if journey.flow_status in (FlowStatus.ACTIVE, FlowStatus.ABANDONED):
            journey.flow_status = FlowStatus.COMPLETED
        closed.append(journey)
        log.info("followup_converted", campaign=campaign.name,
                 customer_id=str(journey.customer_id))
    return closed


def gate(candidate: Candidate, cfg: Journey, now: datetime) -> str | None:
    """Not now, whatever the timing says: they are ordering, or just talked to us."""
    if mid_order(candidate.conversation, now):
        return "ordering_now"
    last = candidate.last_inbound
    if cfg.pause_after_chat_hours and last and last > now - timedelta(hours=cfg.pause_after_chat_hours):
        return "chatted_recently"
    return None


def mid_order(conversation, now: datetime, *, idle=IDLE_STEPS) -> bool:
    if conversation is None:
        return False
    try:
        step = ConversationStep(conversation.step)
    except ValueError:
        return False
    if step in idle:
        return False
    last = aware(conversation.last_message_at) or aware(conversation.updated_at)
    return last is not None and now - last <= timedelta(hours=ACTIVE_HOURS)


# --- which message --------------------------------------------------------------------------
@dataclass
class Pick:
    message: NudgeMessage
    stage: int
    bucket: str
    reason: str
    # Occasion messages passed over for this send: [(code, reason)].
    passed: list | None = None

    @property
    def detail(self) -> str:
        return (f"{self.message.code} · month {self.stage} · "
                f"{BUCKET_LABELS[JourneyBucket(self.bucket)]}")


async def pool_messages(session: AsyncSession, pool: str, cache: dict) -> list[NudgeMessage]:
    """The pool's active messages, once per run."""
    if pool not in cache:
        cache[pool] = [m for m in await library.list_messages(session, pool=pool) if m.is_active]
    return cache[pool]


def _dish(name: str) -> str:
    return " ".join(str(name or "").lower().split())


async def menu_dishes(session: AsyncSession, cache: dict) -> set[str]:
    """Dish names and menu codes on the menu now, once per run."""
    if "__dishes__" not in cache:
        rows = await session.execute(select(MenuItem.name, MenuItem.retailer_id)
                                     .where(MenuItem.is_available.is_(True)))
        cache["__dishes__"] = {_dish(v) for row in rows for v in row if v}
    return cache["__dishes__"]


def needs_coupon(message: NudgeMessage) -> bool:
    try:
        return any(name in COUPON_PLACEHOLDERS for name in placeholders_in(message.body))
    except NudgeConfigError:
        return False


def _angle(text: str | None) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())


def is_occasion(message: NudgeMessage) -> bool:
    return bool(message.starts_on or message.ends_on)


def occasion_problem(message: NudgeMessage, *, today: date, kitchen, dishes: set[str]) -> str | None:
    """Why an occasion (or area- or cutoff-bound) message cannot go now."""
    if message.order_by and today > message.order_by:
        return "occasion_cutoff_passed"
    if message.kitchens and (kitchen is None or kitchen.code not in message.kitchens):
        return "occasion_not_in_area"
    if message.dishes and not ({_dish(d) for d in message.dishes} & dishes):
        return "occasion_dish_unavailable"
    return None


async def pick(session: AsyncSession, cfg: Journey, candidate: Candidate, *, now: datetime,
               window_open: bool, has_coupon: bool, cache: dict, kitchen=None) -> Pick | str:
    """The message for this attempt, or why there is none.

    The choice is seeded by the customer and the attempt, so a preview shows
    the very message the send will use, and a retry sends the same one.
    `kitchen` is theirs, for an occasion message's service area.
    """
    journey = candidate.journey
    bucket = str(journey.bucket if journey else JourneyBucket.NO_RESPONSE)
    stage = stage_of(journey, cfg, now)
    messages = await pool_messages(session, cfg.pool, cache)
    if not messages:
        return "no_message_available"
    dishes = await menu_dishes(session, cache) if any(m.dishes for m in messages) else set()
    today = now.date()
    fits, passed = [], []
    for m in messages:
        if (m.buckets and bucket not in m.buckets) or (m.starts_on and today < m.starts_on) \
                or (m.ends_on and today > m.ends_on) or (not has_coupon and needs_coupon(m)):
            continue
        problem = occasion_problem(m, today=today, kitchen=kitchen, dishes=dishes)
        if problem:
            if is_occasion(m):
                passed.append((m.code, problem))     # logged: the handoff asks why
            continue
        fits.append(m)
    if not window_open:
        # Outside the 24-hour window only an approved template gets through.
        usable = [m for m in fits if m.wa_template and await template_status.is_approved(m.wa_template)]
        if fits and not usable:
            return "no_approved_template"
        fits = usable
    if not fits:
        return passed[0][1] if passed else "no_message_available"

    history = [r.get("code") for r in (journey.recent or [])] if journey else []
    in_order = cfg.pick == "in_order"
    # In order: the next one they have not had. Random: none of the last N.
    avoid = set(history) if in_order else set(history[-cfg.avoid_repeat:] if cfg.avoid_repeat else [])
    # Nor a near-identical angle to their last few.
    angles = {m.code: _angle(m.angle) for m in messages}
    recent_angles = ({angles[c] for c in history[-cfg.avoid_angle_repeat:] if angles.get(c)}
                     if cfg.avoid_angle_repeat else set())
    rng = random.Random(f"{candidate.customer.id}:{candidate.dedupe_key}")

    def choose(options: list[NudgeMessage]) -> NudgeMessage:
        options = sorted(options, key=library.sort_key)
        return options[0] if in_order else rng.choice(options)

    def fresh(options: list[NudgeMessage], *, angle: bool) -> list[NudgeMessage]:
        return [m for m in options if m.code not in avoid
                and not (angle and _angle(m.angle) and _angle(m.angle) in recent_angles)]

    dated = [m for m in fits if is_occasion(m)]
    plain = [m for m in fits if not is_occasion(m)]
    this_month = [m for m in plain if m.month in (stage, None)]

    chosen, reason = None, ""
    for angle in (True, False):
        same_angle = "" if angle or not recent_angles else "; same angle as a recent one (no other left)"
        if fresh(dated, angle=angle):
            chosen = choose(fresh(dated, angle=angle))
            reason = f"occasion{f': {chosen.occasion}' if chosen.occasion else ''}{same_angle}"
        elif fresh(this_month, angle=angle):
            options = fresh(this_month, angle=angle)
            chosen = choose(options)
            reason = (f"month {stage}, {'next in order' if in_order else f'random of {len(options)}'}"
                      f"{same_angle}")
        elif cfg.borrow_other_months and fresh(plain, angle=angle):
            nearest = min(abs((m.month or stage) - stage) for m in fresh(plain, angle=angle))
            options = [m for m in fresh(plain, angle=angle)
                       if abs((m.month or stage) - stage) == nearest]
            chosen = choose(options)
            reason = f"month {stage} all sent recently; from month {chosen.month}{same_angle}"
        if chosen is not None:
            break
    if chosen is None:
        options = this_month or plain or dated
        last_sent = {code: i for i, code in enumerate(history)}
        chosen = min(sorted(options, key=library.sort_key), key=lambda m: last_sent.get(m.code, -1))
        reason = "all sent recently; the one sent longest ago"
    return Pick(message=chosen, stage=stage, bucket=bucket, reason=reason, passed=passed)


def as_message(chosen: NudgeMessage, base: Message) -> Message:
    """The library message in the campaign's shape (its button, the template)."""
    return Message.model_construct(
        text=chosen.body, button=base.button, button_text=base.button_text,
        button_url=base.button_url,
        template=TemplateMessage.model_construct(
            name=chosen.wa_template, params=list(chosen.wa_params or []),
            button=chosen.wa_button or "none", always=False))


# --- after a send ---------------------------------------------------------------------------
def _new(campaign: NudgeCampaign, customer: Customer, now: datetime) -> NudgeJourney:
    return NudgeJourney(campaign_id=campaign.id, customer_id=customer.id,
                        bucket=JourneyBucket.NO_RESPONSE, status=JourneyStatus.ACTIVE, round=1,
                        started_at=now, attempts=0, bucket_attempts=0, failures=0,
                        next_due_at=now, recent=[])


async def record_sent(session: AsyncSession, campaign: NudgeCampaign, cfg: Journey,
                      candidate: Candidate, chosen: Pick, now: datetime) -> NudgeJourney:
    """Count the send and set the next date. The first send starts the journey."""
    journey = candidate.journey
    if journey is None:
        journey = candidate.journey = _new(campaign, candidate.customer, now)
        session.add(journey)
    journey.attempts += 1
    journey.bucket_attempts += 1
    journey.failures = 0
    journey.last_sent_at = now
    journey.last_message_code = chosen.message.code
    journey.recent = [*(journey.recent or []),
                      {"code": chosen.message.code, "at": now.isoformat()}][-RECENT_KEPT:]
    schedule_next(journey, cfg, now)
    await session.flush()
    return journey


async def record_failed(session: AsyncSession, campaign: NudgeCampaign, cfg: Journey,
                        candidate: Candidate, now: datetime) -> NudgeJourney:
    journey = candidate.journey
    if journey is None:
        journey = candidate.journey = _new(campaign, candidate.customer, now)
        session.add(journey)
    journey.failures += 1
    if journey.failures >= FAILURES_PER_ATTEMPT:
        journey.attempts += 1
        journey.bucket_attempts += 1
        journey.failures = 0
        schedule_next(journey, cfg, now)
    await session.flush()
    return journey


def schedule_next(journey: NudgeJourney, cfg: Journey, now: datetime) -> None:
    days = cfg.cadence(journey.bucket).days_until(journey.bucket_attempts)
    if days is None:
        end(journey, "attempt_limit", now)
        return
    upcoming = now + timedelta(days=days)
    if upcoming >= ends_at(journey, cfg):
        end(journey, "length_reached", now)
        return
    journey.next_due_at = upcoming


def end(journey: NudgeJourney, reason: str, now: datetime) -> None:
    journey.status, journey.end_reason, journey.ended_at = JourneyStatus.ENDED, reason, now


def move(journey: NudgeJourney, bucket: str, cfg: Journey, now: datetime, *,
         replied: bool) -> None:
    """Into another group: its count starts again, and so does its wait."""
    bucket = str(JourneyBucket(bucket))
    journey.bucket = bucket
    journey.bucket_attempts = 0
    journey.next_due_at = now + timedelta(days=cfg.cadence(bucket).days_until(0) or 0)
    if replied:
        journey.last_reply, journey.last_reply_at = bucket, now


def next_after_send(journey: NudgeJourney) -> str:
    if journey.status != JourneyStatus.ACTIVE:
        return f"none - {END_REASONS.get(journey.end_reason or '', 'ended').lower()}"
    return f"{aware(journey.next_due_at):%d %b %Y}"


# --- the ordering flow ---------------------------------------------------------------------------
async def flow_started(session: AsyncSession, customer: Customer, keyword: str,
                       now: datetime | None = None) -> int:
    """ORDER FOOD (a trigger keyword) from someone on a follow-up journey.

    Their follow-ups wait for the order (Engaged); the follow-up they were
    answering is credited with the keyword. Returns how many journeys.
    """
    now = now or utcnow()
    rows = await session.execute(select(NudgeJourney).where(
        NudgeJourney.customer_id == customer.id,
        NudgeJourney.status.in_([JourneyStatus.ACTIVE, JourneyStatus.PAUSED])))
    journeys = list(rows.scalars())
    if not journeys:
        return 0
    for journey in journeys:
        journey.flow_status = FlowStatus.ACTIVE
        journey.flow_started_at = now
        journey.last_keyword = keyword[:60]
    answered = (await session.execute(
        select(NudgeSend).where(NudgeSend.customer_id == customer.id,
                                NudgeSend.kind == NudgeKind.FOLLOW_UP,
                                NudgeSend.status == NudgeStatus.SENT,
                                NudgeSend.sent_at >= now - timedelta(days=KEYWORD_CREDIT_DAYS),
                                NudgeSend.keyword_at.is_(None))
        .order_by(NudgeSend.sent_at.desc()).limit(1))).scalar_one_or_none()
    if answered is not None:
        answered.keyword_at = now
    await session.flush()
    from app.services.nudges import crm_fields

    await note_in_zoho(session, customer, "Follow-up: ORDER FOOD received",
                       f"{keyword} at {now:%d %b %Y %H:%M} UTC. Follow-ups wait for the order"
                       + (f" (answering {answered.message_code})." if answered and answered.message_code
                          else "."))
    for journey in journeys:
        await crm_fields.sync(session, customer, journey)
    log.info("followup_flow_started", customer_id=str(customer.id), keyword=keyword,
             journeys=len(journeys))
    return len(journeys)


# A keyword up to this long after a follow-up is credited to it.
KEYWORD_CREDIT_DAYS = 7


# --- the customer's reply -------------------------------------------------------------------
def _normal(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())


async def reply_word(session: AsyncSession, message: str) -> str | None:
    """"later" or "no" if the whole message is one of those keywords."""
    text = _normal(message)
    if not text or len(text) > 40:
        return None
    _, settings = await store.load_settings(session)
    if text in settings.later_keywords:
        return "later"
    if text in settings.no_keywords:
        return "no"
    return None


async def answerable(session: AsyncSession, customer: Customer) -> list[NudgeJourney]:
    """Their running journeys that have written to them: what a reply answers."""
    rows = await session.execute(select(NudgeJourney).where(
        NudgeJourney.customer_id == customer.id, NudgeJourney.status == JourneyStatus.ACTIVE,
        NudgeJourney.last_sent_at.is_not(None)))
    return list(rows.scalars())


async def is_reply(session: AsyncSession, number: str, message: str) -> bool:
    """Asked by reply_triggers: LATER / NO from someone a follow-up wrote to."""
    if await reply_word(session, message) is None:
        return False
    customer = await get_customer(session, number)
    return customer is not None and bool(await answerable(session, customer))


async def handle_reply(ctx) -> bool:
    """LATER / NO to a follow-up, from the conversation engine. True if it was one.

    Only from someone a follow-up has written to and who is not part-way
    through an order - there "no" answers the bot's own question.
    """
    which = await reply_word(ctx.session, ctx.choice)
    if which is None:
        return False
    now = utcnow()
    if mid_order(ctx.conversation, now,
                 idle=IDLE_STEPS | {ConversationStep.MAIN_MENU}) \
            or ctx.conversation.step == ConversationStep.HANDED_OVER:
        return False
    journeys = await answerable(ctx.session, ctx.customer)
    if not journeys:
        return False

    _, settings = await store.load_settings(ctx.session)
    if which == "no" and settings.no_means_opt_out:
        from app.services.nudges import optout

        for journey in journeys:
            end(journey, "declined", now)
            journey.last_keyword = (ctx.choice or which).strip()[:60]
        await optout.opt_out(ctx.session, ctx.customer, source="whatsapp")
        await ctx.reply_text(settings.opt_out_reply)
        log.info("followup_reply_opt_out", customer_id=str(ctx.customer.id))
        return True

    names = []
    for journey in journeys:
        campaign = await ctx.session.get(NudgeCampaign, journey.campaign_id)
        cfg = journey_config(campaign)
        move(journey, which, cfg, now, replied=True)
        journey.last_keyword = (ctx.choice or which).strip()[:60]
        names.append(f"{campaign.name if campaign else 'Follow-up'}: next {next_after_send(journey)}")
    await ctx.session.flush()
    await ctx.reply_text(settings.later_reply if which == "later" else settings.no_reply)
    label = BUCKET_LABELS[JourneyBucket(which)]
    await note_in_zoho(ctx.session, ctx.customer, f"Follow-up: replied {label}",
                       f"Moved to the {label} group ({now:%d %b %Y %H:%M} UTC).\n"
                       + "\n".join(names))
    from app.services.nudges import crm_fields

    for journey in journeys:
        await crm_fields.sync(ctx.session, ctx.customer, journey)
    log.info("followup_reply", customer_id=str(ctx.customer.id), bucket=which,
             journeys=len(journeys))
    return True


async def note_in_zoho(session: AsyncSession, customer: Customer, title: str,
                       content: str) -> None:
    """Best effort, as for every nudge: Zoho being down changes nothing here."""
    from app.services.nudges.runner import zoho_record

    _, settings = await store.load_settings(session)
    record = zoho_record(customer)
    if not (settings.zoho_sync and settings.zoho_notes and record):
        return
    try:
        await engagement.add_note(*record, title=title, content=content)
    except (IntegrationError, ConfigurationError) as exc:
        log.warning("followup_zoho_note_failed", customer_id=str(customer.id), error=str(exc))


# --- held back ---------------------------------------------------------------------------------
async def log_skip(session: AsyncSession, campaign: NudgeCampaign, candidate: Candidate,
                   reason: str, now: datetime, *, message_code: str = "") -> None:
    """A due follow-up held back (or an occasion passed over), counted per day."""
    reason = reason.split(":")[0][:60]
    if not message_code and reason in NOT_DUE:
        return
    day = now.date()
    row = (await session.execute(select(NudgeSkip).where(
        NudgeSkip.campaign_id == campaign.id, NudgeSkip.customer_id == candidate.customer.id,
        NudgeSkip.day == day, NudgeSkip.reason == reason,
        NudgeSkip.message_code == (message_code or "")))).scalar_one_or_none()
    if row is None:
        session.add(NudgeSkip(campaign_id=campaign.id, customer_id=candidate.customer.id,
                              day=day, reason=reason, message_code=message_code or "", count=1))
    else:
        row.count += 1
    journey = candidate.journey
    if journey is not None and not message_code:
        journey.last_skip_reason, journey.last_skip_at = reason, now
    await session.flush()


# --- the team's controls and views ------------------------------------------------------------
ACTIONS = ("move", "pause", "resume", "restart", "end", "due_now")


async def get_journey(session: AsyncSession, journey_id) -> NudgeJourney | None:
    import uuid

    try:
        return await session.get(NudgeJourney, uuid.UUID(str(journey_id)))
    except (TypeError, ValueError):
        return None


async def act(session: AsyncSession, journey: NudgeJourney, action: str, *,
              bucket: str | None = None, actor: str) -> NudgeJourney:
    if action not in ACTIONS:
        raise NudgeConfigError(f"action is one of {list(ACTIONS)}")
    campaign = await session.get(NudgeCampaign, journey.campaign_id)
    cfg = journey_config(campaign)
    now = utcnow()
    if action == "move":
        try:
            move(journey, JourneyBucket(str(bucket or "")), cfg, now, replied=False)
        except ValueError:
            raise NudgeConfigError(f"bucket is one of {[str(b) for b in JourneyBucket]}") from None
    elif action == "pause":
        if journey.status != JourneyStatus.ACTIVE:
            raise NudgeConfigError("Only a running journey can be paused")
        journey.status = JourneyStatus.PAUSED
    elif action == "resume":
        if journey.status != JourneyStatus.PAUSED:
            raise NudgeConfigError("Only a paused journey can be resumed")
        journey.status = JourneyStatus.ACTIVE
    elif action == "restart":
        journey.round += 1
        journey.status, journey.bucket = JourneyStatus.ACTIVE, JourneyBucket.NO_RESPONSE
        journey.attempts = journey.bucket_attempts = journey.failures = 0
        journey.started_at = journey.next_due_at = now
        journey.end_reason = journey.ended_at = None
        journey.flow_status, journey.flow_started_at = FlowStatus.NOT_STARTED, None
    elif action == "end":
        end(journey, "ended_by_team", now)
    elif action == "due_now":
        if journey.status not in (JourneyStatus.ACTIVE, JourneyStatus.PAUSED):
            raise NudgeConfigError("This journey has ended; restart it instead")
        journey.next_due_at = now
    await session.flush()
    log.info("followup_journey_changed", action=action, bucket=bucket, actor=actor,
             journey_id=str(journey.id))
    customer = await session.get(Customer, journey.customer_id)
    if customer is not None:
        from app.services.nudges import crm_fields

        await crm_fields.sync(session, customer, journey)
    return journey


def journey_view(journey: NudgeJourney, campaign: NudgeCampaign | None, now: datetime) -> dict:
    cfg = journey_config(campaign)
    running = journey.status in (JourneyStatus.ACTIVE, JourneyStatus.PAUSED)
    return {
        "id": str(journey.id),
        "campaign_id": str(journey.campaign_id),
        "campaign": campaign.name if campaign else None,
        "bucket": journey.bucket,
        "bucket_label": BUCKET_LABELS.get(JourneyBucket(journey.bucket), journey.bucket),
        "status": journey.status,
        "round": journey.round,
        "attempts": journey.attempts,
        "bucket_attempts": journey.bucket_attempts,
        "stage": stage_of(journey, cfg, now),
        "stages": cfg.stages,
        "started_at": aware(journey.started_at).isoformat(),
        "ends_at": ends_at(journey, cfg).isoformat(),
        "next_due_at": aware(journey.next_due_at).isoformat() if running else None,
        "last_sent_at": aware(journey.last_sent_at).isoformat() if journey.last_sent_at else None,
        "last_message_code": journey.last_message_code,
        "last_reply": journey.last_reply,
        "last_reply_at": aware(journey.last_reply_at).isoformat() if journey.last_reply_at else None,
        "end_reason": journey.end_reason,
        "end_reason_text": END_REASONS.get(journey.end_reason or "", journey.end_reason),
        "ended_at": aware(journey.ended_at).isoformat() if journey.ended_at else None,
        "crm_bucket": crm_bucket(journey),
        "flow_status": journey.flow_status,
        "flow_label": FLOW_LABELS.get(journey.flow_status, journey.flow_status),
        "flow_started_at": aware(journey.flow_started_at).isoformat() if journey.flow_started_at else None,
        "last_keyword": journey.last_keyword,
        "timezone": journey.timezone,
        "timezone_source": journey.timezone_source,
        "last_skip_reason": journey.last_skip_reason,
        "last_skip_at": aware(journey.last_skip_at).isoformat() if journey.last_skip_at else None,
        "recent": list(journey.recent or [])[-10:],
        "zoho_synced_at": aware(journey.zoho_synced_at).isoformat() if journey.zoho_synced_at else None,
        "zoho_error": journey.zoho_error,
    }


async def customer_journeys(session: AsyncSession, customer: Customer) -> list[dict]:
    now = utcnow()
    rows = (await session.execute(select(NudgeJourney).where(
        NudgeJourney.customer_id == customer.id).order_by(NudgeJourney.created_at))).scalars()
    return [journey_view(j, await session.get(NudgeCampaign, j.campaign_id), now) for j in rows]


async def summary(session: AsyncSession) -> dict[str, dict]:
    """For each follow-up campaign: who is where - by group, month, status."""
    now = utcnow()
    campaigns = {c.id: c for c in await store.list_campaigns(session)
                 if c.kind == NudgeKind.FOLLOW_UP}
    out = {str(cid): {"total": 0, "by_status": Counter(), "by_bucket": Counter(),
                      "by_stage": Counter(), "end_reasons": Counter(), "due_now": 0,
                      "by_crm_bucket": Counter(), "flow": Counter(), "dead_days": []}
           for cid in campaigns}
    if not campaigns:
        return {}
    configs = {cid: journey_config(c) for cid, c in campaigns.items()}
    rows = (await session.execute(select(NudgeJourney).where(
        NudgeJourney.campaign_id.in_(list(campaigns))))).scalars()
    for journey in rows:
        entry = out[str(journey.campaign_id)]
        entry["total"] += 1
        entry["by_status"][journey.status] += 1
        entry["by_crm_bucket"][crm_bucket(journey)] += 1
        if journey.flow_started_at:
            entry["flow"][journey.flow_status] += 1
        if crm_bucket(journey) == "Dead Lead" and journey.ended_at:
            entry["dead_days"].append((aware(journey.ended_at) - aware(journey.started_at)).days)
        if journey.status == JourneyStatus.ACTIVE:
            entry["by_bucket"][journey.bucket] += 1
            entry["by_stage"][stage_of(journey, configs[journey.campaign_id], now)] += 1
            if aware(journey.next_due_at) <= now:
                entry["due_now"] += 1
        elif journey.end_reason:
            entry["end_reasons"][journey.end_reason] += 1
    for entry in out.values():
        days = entry.pop("dead_days")
        entry["dead_avg_days"] = round(sum(days) / len(days), 1) if days else None
    return {cid: {k: dict(v) if isinstance(v, Counter) else v for k, v in entry.items()}
            for cid, entry in out.items()}
