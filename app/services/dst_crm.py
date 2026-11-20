"""DST India's own CRM: its Lead Pipeline & Tracker at dst.shero.website.

DST India does not use Zoho for this. Its CRM (a Node app on DST's own server)
has 13 pipeline stages, keyed as the CRM keys them:

    welcome, requirementCapture, rnr1, rnr2, rnrFinal, quotationSent,
    quotationFollowup, quotationExpiring, revisedQuotation, orderConfirmed,
    deliveryStatus, lostFeedback, winback

The ordering bot ("Hungry Now? Order Food", the CRM's "Order Now" vertical)
sends it everything that happens to an ordering customer, through the CRM's
ordering-bot endpoint (deploy/dst-crm-endpoint/, POST
/api/integrations/ordering-bot/events):

    lead.stage      each step of the ordering funnel
                      New Enquiry                  -> welcome            NEW
                                                      (+ a note: the original source,
                                                       Meta ad or the DST number)
                      Details Captured             -> requirementCapture CONTACTED
                      cart, address, time, link,
                      abandoned or failed payment  -> requirementCapture IN_DISCUSSION
                      Not Serviceable              -> a note on the lead
                      Mark ready (rider booked)    -> deliveryStatus + a note
                                                      (courier, AWB, fee, tracking)
                      rider booking cancelled      -> a note
    order.paid      the paid order, its dishes and the contact
                                                   -> orderConfirmed     WON
    order.status    sent to kitchen                -> orderConfirmed
                    out for delivery, delivered    -> deliveryStatus
                    cancelled, refunded            -> the order cancelled
    order.feedback  the rating after delivery      -> a note
    nudge           each follow-up sent, and the button tapped on it
                      No Response follow-up 1/2/3+ -> rnr1 / rnr2 / rnrFinal
                      a template named rnr_1, rnr_2, rnr_final, win_back
                                                   -> that stage
                      No, STOP                     -> lostFeedback       LOST

This follows DST's lead design (DST_CRM_Lead_Architecture_and_WhatsApp_Flows,
19 Nov 2026) for the Order Now branch: the lead enters at Welcome with its
source; Requirement Capture comes from the customer's own answers, never from
a message sent; Order Confirmed from a verified payment; Delivery Status from
the dispatch (the rider booked) and the delivery steps; RNR from no-response
follow-ups; Lost from No / STOP. One lead per customer, updated, never a
second one. A test-payment order is labelled as such.

Each event is written to the outbox (models/crm_outbox.py) in the same
transaction as the change it reports, and the `crm_outbox` job posts them, in
order per customer, until the CRM takes them. A conversation never waits on
the CRM, and the CRM being down or redeployed loses nothing.

Only DST India, and only while DST_CRM_API_URL and DST_CRM_API_TOKEN are set.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_object_session

from app.core import tenants
from app.core.config import is_unset, settings
from app.core.logging import get_logger
from app.db.models import Customer, CrmOutbox, LeadStage, NudgeBucket, NudgeSend, Order, OrderStage, Outlet
from app.db.models.crm_outbox import FAILED, PENDING, SENT

log = get_logger(__name__)

# --- the CRM's pipeline stages (pipeline_stages.key) ---------------------------------------
WELCOME = "welcome"
REQUIREMENT = "requirementCapture"
RNR1 = "rnr1"
RNR2 = "rnr2"
RNR_FINAL = "rnrFinal"
QUOTE_SENT = "quotationSent"
QUOTE_FOLLOWUP = "quotationFollowup"
QUOTE_EXPIRING = "quotationExpiring"
REVISED = "revisedQuotation"
CONFIRMED = "orderConfirmed"
DELIVERY = "deliveryStatus"
LOST = "lostFeedback"
WINBACK = "winback"

# Key -> the name the CRM shows.
STAGES: dict[str, str] = {
    WELCOME: "Welcome", REQUIREMENT: "Requirement Capture", RNR1: "RNR 1", RNR2: "RNR 2",
    RNR_FINAL: "RNR Final", QUOTE_SENT: "Quotation Sent", QUOTE_FOLLOWUP: "Quotation Follow-up",
    QUOTE_EXPIRING: "Quotation Expiring", REVISED: "Revised Quotation",
    CONFIRMED: "Order Confirmed", DELIVERY: "Delivery Status", LOST: "Lost Feedback",
    WINBACK: "Win-back",
}

# The vertical the CRM files these leads under.
VERTICAL = "orderNow"

FOR_LEAD_STAGE: dict[str, str] = {
    LeadStage.NEW_ENQUIRY: WELCOME,
    LeadStage.DETAILS_CAPTURED: REQUIREMENT,
    LeadStage.CUISINE_SELECTED: REQUIREMENT,
    LeadStage.CART_CREATED: REQUIREMENT,
    LeadStage.OUTLET_SELECTED: REQUIREMENT,
    LeadStage.SLOT_SELECTED: REQUIREMENT,
    LeadStage.PAYMENT_LINK_SENT: REQUIREMENT,
    LeadStage.PAYMENT_ABANDONED: REQUIREMENT,
    LeadStage.PAYMENT_FAILED: REQUIREMENT,
    LeadStage.CONVERTED: CONFIRMED,
    # Not Serviceable has no stage of its own: it goes as a note on the lead.
}

# The CRM's own lead status (its LeadStage enum) for each of ours.
CRM_STATUS: dict[str, str] = {
    LeadStage.NEW_ENQUIRY: "NEW",
    LeadStage.DETAILS_CAPTURED: "CONTACTED",
    LeadStage.CUISINE_SELECTED: "IN_DISCUSSION",
    LeadStage.CART_CREATED: "IN_DISCUSSION",
    LeadStage.OUTLET_SELECTED: "IN_DISCUSSION",
    LeadStage.SLOT_SELECTED: "IN_DISCUSSION",
    LeadStage.PAYMENT_LINK_SENT: "IN_DISCUSSION",
    LeadStage.PAYMENT_ABANDONED: "IN_DISCUSSION",
    LeadStage.PAYMENT_FAILED: "IN_DISCUSSION",
    LeadStage.CONVERTED: "WON",
}

FOR_ORDER_STAGE: dict[str, str] = {
    OrderStage.PAID_SLOT_BOOKED: CONFIRMED,
    OrderStage.SENT_TO_KITCHEN: CONFIRMED,
    OrderStage.OUT_FOR_DELIVERY: DELIVERY,
    OrderStage.DELIVERED: DELIVERY,
    # Pending Payment never reaches the CRM; Cancelled and Refunded change
    # the order, not the lead's stage.
}

# The CRM's order status (its OrderStatus enum) for each of ours.
ORDER_STATUS: dict[str, str] = {
    OrderStage.PENDING_PAYMENT: "PENDING",
    OrderStage.PAID_SLOT_BOOKED: "CONFIRMED",
    OrderStage.SENT_TO_KITCHEN: "CONFIRMED",
    OrderStage.OUT_FOR_DELIVERY: "CONFIRMED",
    OrderStage.DELIVERED: "DELIVERED",
    OrderStage.CANCELLED: "CANCELLED",
    OrderStage.REFUNDED: "CANCELLED",
}

# A nudge template named after a CRM stage puts the lead in it (DST's
# approved follow-ups are dst_rnr_1, dst_rnr_2, dst_rnr_final, dst_win_back).
TEMPLATE_STAGES: tuple[tuple[str, str], ...] = (
    ("rnr_final", RNR_FINAL), ("rnrfinal", RNR_FINAL),
    ("rnr_1", RNR1), ("rnr1", RNR1), ("rnr_2", RNR2), ("rnr2", RNR2),
    ("win_back", WINBACK), ("winback", WINBACK), ("lost_feedback", LOST),
)

EVENTS_PATH = "/api/integrations/ordering-bot/events"
HEALTH_PATH = "/api/integrations/ordering-bot/health"
TIMEOUT = httpx.Timeout(15.0, connect=10.0)
BATCH = 50
# Minutes before the next try after the 1st, 2nd... failure; then every 6 hours.
BACKOFF_MINUTES = (1, 2, 5, 10, 30, 60, 120, 240)
GIVE_UP_AFTER = timedelta(days=7)
# A refusal that sending again cannot change. 401/403 (a wrong or rotated
# key) and 404 (the CRM redeployed without the endpoint) are kept and retried:
# they are fixed on the CRM's side, and then everything waiting goes through.
PERMANENT = {400, 413, 422}


def in_use() -> bool:
    """The DST CRM is DST India's: only while serving it."""
    return tenants.current() == tenants.DST


def connected() -> bool:
    """DST India, with the CRM's address and key set."""
    return in_use() and not is_unset(settings.crm_api_url) and not is_unset(settings.crm_api_token)


def stage_for(lead_stage: str | LeadStage | None) -> str | None:
    """The CRM pipeline stage for one of our funnel stages, or None."""
    return FOR_LEAD_STAGE.get(str(lead_stage)) if lead_stage else None


def stage_for_order(order_stage: str | OrderStage | None) -> str | None:
    """The CRM pipeline stage for one of our order stages, or None."""
    return FOR_ORDER_STAGE.get(str(order_stage)) if order_stage else None


def stage_for_nudge(send: NudgeSend, reply: str | None = None) -> str | None:
    """Where a nudge puts the lead: No / STOP -> Lost Feedback; a template
    named after a stage -> that stage; the No Response follow-ups -> RNR 1,
    RNR 2, then RNR Final. A stage message or a Later follow-up leaves the
    lead where its ordering stage put it."""
    if reply:
        return LOST if reply.strip().lower() in ("no", "stop") else None
    if send.status != "sent":
        return None
    name = (send.template or "").lower()
    for marker, stage in TEMPLATE_STAGES:
        if marker in name:
            return stage
    if send.kind == NudgeBucket.NO_RESPONSE:
        return {1: RNR1, 2: RNR2}.get(send.attempt, RNR_FINAL)
    if send.kind in (NudgeBucket.NO, NudgeBucket.DEAD):
        return LOST
    return None


# --- what is sent ---------------------------------------------------------------------------
TEST_PAYMENT = "TEST - simulated payment, nothing charged"


def source_note(customer: Customer) -> str:
    """Where the lead came from, for its Welcome note (the design's "identify
    the original source"): a Meta Click-to-WhatsApp ad, or the DST number."""
    if (customer.lead_source or "").lower().startswith("meta") or customer.ad_id or customer.campaign_id:
        referral = customer.referral_payload or {}
        parts = ["Source: Meta ad (Click-to-WhatsApp)"]
        if customer.ad_id:
            parts.append(f"ad {customer.ad_id}")
        if customer.campaign_id:
            parts.append(f"campaign {customer.campaign_id}")
        headline = str(referral.get("headline") or "").strip()
        if headline:
            parts.append(f'"{headline[:80]}"')
        text = " · ".join(parts)
    else:
        text = "Source: WhatsApp, DST INDIA number"
    return text + " - Order Now branch (Hungry Now? Order Food)."


def _amount(value) -> str:
    return f"{Decimal(str(value or 0)):.2f}"


def _float(value) -> float | None:
    return float(value) if value is not None else None


def _address(*parts: str | None) -> str | None:
    text = ", ".join(p.strip() for p in parts if p and p.strip())
    return text or None


def customer_fields(customer: Customer) -> dict:
    return {
        "id": str(customer.id),
        "phone": customer.whatsapp_number,
        "name": customer.name or customer.whatsapp_profile_name,
        "email": customer.email,
        "address": _address(customer.address_line1, customer.apartment_unit),
        "pincode": customer.postal_code,
    }


def order_fields(order: Order, *, kitchen: str | None = None) -> dict:
    """The whole order: every order event carries it, so the CRM can make the
    order from any one of them."""
    items = []
    for line in order.items or []:
        quantity = int(line.get("quantity") or 0)
        if quantity < 1:
            continue
        rate = Decimal(str(line.get("unit_price") or 0))
        items.append({
            "name": str(line.get("name") or line.get("retailer_id") or "Dish"),
            "sku": line.get("retailer_id"),
            "quantity": quantity,
            "rate": _amount(rate),
            "amount": _amount(rate * quantity),
        })
    return {
        "number": order.order_number,
        "stage": order.stage,
        "crm_status": ORDER_STATUS.get(order.stage),
        "items": items,
        "subtotal": _amount(order.dish_total),
        "delivery_charge": _amount(Decimal(str(order.delivery_fee or 0)) + Decimal(str(order.extra_fees or 0))),
        "discount": _amount(Decimal(str(order.discount or 0)) + Decimal(str(order.loyalty_discount or 0))),
        "discount_code": order.promo_code,
        "tax": _amount(order.tax),
        "total": _amount(order.total),
        "delivery_date": order.slot_starts_at.isoformat() if order.slot_starts_at else None,
        "slot_label": order.slot_label,
        "address": _address(order.delivery_address, order.apartment_unit),
        "pincode": order.postal_code,
        "latitude": _float(order.delivery_latitude),
        "longitude": _float(order.delivery_longitude),
        "kitchen": kitchen,
        # A test-payment order (Settings: Test payments) says so wherever the
        # CRM shows its payment: nothing was charged.
        "payment_reference": order.stripe_payment_intent_id or (
            TEST_PAYMENT if (order.stripe_session_id or "").startswith("mock_") else None),
        "instructions": order.delivery_instructions,
        "refund_amount": _amount(order.refund_amount) if order.refund_amount else None,
        "feedback": order.feedback_rating,
    }


async def _kitchen_name(session: AsyncSession, order: Order) -> str | None:
    if not order.outlet_id:
        return None
    outlet = await session.get(Outlet, order.outlet_id)
    return outlet.name if outlet else None


async def _enqueue(session: AsyncSession | None, customer: Customer, event: str, body: dict, *,
                   dedupe: str | None = None, now: datetime | None = None) -> CrmOutbox | None:
    """Add one event to the outbox, in the caller's transaction. None when the
    CRM is not connected, there is no session, or it is already queued."""
    if not connected():
        return None
    if session is None:
        log.warning("dst_crm_no_session", kind=event, customer_id=str(customer.id))
        return None
    if dedupe and (await session.execute(
            select(CrmOutbox.id).where(CrmOutbox.dedupe_key == dedupe))).first():
        return None
    now = now or datetime.now(timezone.utc)
    row = CrmOutbox(
        id=uuid.uuid4(), event=event, customer_id=customer.id, dedupe_key=dedupe, status=PENDING,
        attempts=0, next_attempt_at=now, queued_at=now,
        payload={"event": event, "occurred_at": now.isoformat(),
                 "customer": customer_fields(customer), **body},
    )
    row.payload["event_id"] = str(row.id)
    session.add(row)
    return row


# --- the events -----------------------------------------------------------------------------
async def record_new_enquiry(customer: Customer) -> str | None:
    """Their first message (onboarding.start): the lead, at Welcome / NEW.
    A customer starts at New Enquiry, so advance_stage never reports it; this
    does, once per customer."""
    if not in_use() or str(customer.lead_stage) != LeadStage.NEW_ENQUIRY:
        return None
    await _enqueue(async_object_session(customer), customer, "lead.stage", {
        "stage": {"ours": LeadStage.NEW_ENQUIRY.value, "pipeline": WELCOME, "crm": "NEW",
                  "note": source_note(customer)}}, dedupe=f"lead.new:{customer.id}")
    return WELCOME


async def record_dispatch(customer: Customer | None, order: Order, *, booked: bool) -> str | None:
    """The dispatch event (Mark ready booked a Shiprocket rider): the lead to
    Delivery Status, with the courier, AWB, fee and tracking link as a note.
    A cancelled booking is a note only. Delivered and Out for delivery still
    come from the order's own steps (record_order_stage)."""
    if not in_use() or customer is None:
        return None
    info = dict(order.courier or {})
    if booked:
        bits = [f"Shiprocket rider booked for {order.order_number}"]
        if info.get("courier_name"):
            bits.append(str(info["courier_name"]))
        if info.get("awb"):
            bits.append(f"AWB {info['awb']}")
        if info.get("fee") is not None:
            bits.append(f"rider fee ₹{float(info['fee']):.0f}")
        note = " · ".join(bits) + (f"\nTrack: {info['tracking_url']}" if info.get("tracking_url") else "")
        stage = {"ours": "Rider booked", "pipeline": DELIVERY, "note": note}
        key = f"order.dispatch:{order.order_number}:{info.get('order_id') or info.get('shipment_id')}"
    else:
        last = (info.get("cancelled") or [{}])[-1]
        stage = {"ours": "Rider cancelled", "pipeline": None,
                 "note": f"Shiprocket rider booking cancelled for {order.order_number}"
                         + (f" (AWB {last['awb']})" if last.get("awb") else "")}
        key = f"order.dispatch.cancel:{order.order_number}:{last.get('order_id')}"
    await _enqueue(async_object_session(order) or async_object_session(customer), customer,
                   "lead.stage", {"stage": stage}, dedupe=key)
    return stage["pipeline"]


async def record_lead_stage(customer: Customer, lead_stage: str | LeadStage) -> str | None:
    """Our funnel moved (crm_sync.advance_stage): its CRM stage, queued. None
    when this is not DST India or the stage has no pipeline stage."""
    if not in_use():
        return None
    stage = stage_for(lead_stage)
    note = None
    if str(lead_stage) == LeadStage.NOT_SERVICEABLE:
        where = _address(customer.address_line1, customer.postal_code)
        note = ("No kitchen delivers to " + where) if where else "No kitchen delivers to the address given"
    if stage or note:
        await _enqueue(async_object_session(customer), customer, "lead.stage", {
            "stage": {"ours": str(lead_stage), "pipeline": stage,
                      "crm": CRM_STATUS.get(str(lead_stage)), "note": note}})
    return stage


async def record_paid_order(customer: Customer, order: Order, *, kitchen: str | None = None) -> str | None:
    """Paid (crm_sync.convert_and_record_order): the order, its dishes and the
    contact, and the lead to Order Confirmed / WON."""
    if not in_use():
        return None
    session = async_object_session(order) or async_object_session(customer)
    if kitchen is None and session is not None:
        kitchen = await _kitchen_name(session, order)
    await _enqueue(session, customer, "order.paid", {
        "stage": {"ours": LeadStage.CONVERTED.value, "pipeline": CONFIRMED, "crm": "WON"},
        "order": order_fields(order, kitchen=kitchen)}, dedupe=f"order.paid:{order.order_number}")
    return CONFIRMED


async def record_order_stage(customer: Customer | None, order: Order,
                             order_stage: str | OrderStage) -> str | None:
    """A paid order moved on (crm_sync.push_order_stage): its status, and the
    lead's pipeline stage."""
    if not in_use() or customer is None:
        return None
    if str(order_stage) == OrderStage.PENDING_PAYMENT:
        return None
    session = async_object_session(order) or async_object_session(customer)
    kitchen = await _kitchen_name(session, order) if session is not None else None
    stage = stage_for_order(order_stage)
    fields = order_fields(order, kitchen=kitchen)
    fields["stage"] = str(order_stage)
    fields["crm_status"] = ORDER_STATUS.get(str(order_stage))
    await _enqueue(session, customer, "order.status", {
        "stage": {"ours": str(order_stage), "pipeline": stage} if stage else None,
        "order": fields}, dedupe=f"order.status:{order.order_number}:{order_stage}")
    return stage


async def record_feedback(customer: Customer, order: Order, rating: str) -> None:
    """The customer rated a delivered order."""
    if not in_use():
        return
    session = async_object_session(order) or async_object_session(customer)
    fields = order_fields(order)
    fields["feedback"] = rating
    await _enqueue(session, customer, "order.feedback", {"order": fields},
                   dedupe=f"order.feedback:{order.order_number}")


async def record_nudge(customer: Customer, send: NudgeSend, *, bucket: str | None = None,
                       reply: str | None = None, summary: str | None = None) -> str | None:
    """A nudge went (or failed), or the customer tapped a button on one."""
    if not in_use():
        return None
    stage = stage_for_nudge(send, reply)
    crm = "LOST" if stage == LOST else None
    session = async_object_session(send) or async_object_session(customer)
    await _enqueue(session, customer, "nudge", {
        "stage": {"pipeline": stage, "crm": crm} if stage else None,
        "nudge": {"kind": send.kind, "attempt": send.attempt, "template": send.template,
                  "status": send.status, "reply": reply, "bucket": bucket or send.kind,
                  "provider_message_id": None if reply else send.provider_message_id,
                  "summary": summary or send.error}})
    return stage


# --- sending -------------------------------------------------------------------------------
class _Retry(Exception):
    pass


class _Refused(Exception):
    pass


def _base_url() -> str:
    return settings.crm_api_url.rstrip("/")


def _headers() -> dict:
    return {"x-ordering-bot-token": settings.crm_api_token, "content-type": "application/json"}


def _message(response: httpx.Response) -> str:
    try:
        return str(response.json().get("message") or response.text)[:300]
    except ValueError:
        return response.text[:300]


async def post_event(payload: dict) -> dict:
    """POST one event to the CRM. Raises _Retry (try again later) or _Refused
    (the CRM will never take it)."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(_base_url() + EVENTS_PATH, json=payload, headers=_headers())
    except httpx.HTTPError as exc:
        raise _Retry(f"CRM not reachable: {exc.__class__.__name__}: {exc}"[:300]) from exc
    if response.status_code < 300:
        try:
            return response.json()
        except ValueError:
            return {}
    reason = f"CRM answered {response.status_code}: {_message(response)}"
    if response.status_code in PERMANENT:
        raise _Refused(reason)
    raise _Retry(reason)


def _backoff(attempts: int) -> timedelta:
    minutes = BACKOFF_MINUTES[attempts - 1] if attempts <= len(BACKOFF_MINUTES) else 360
    return timedelta(minutes=minutes)


async def flush(session: AsyncSession, *, now: datetime | None = None, limit: int = BATCH) -> dict:
    """Send what is due, oldest first. A customer whose earlier event did not
    go through waits, so the CRM always sees each customer's events in order."""
    counts = {"sent": 0, "retry": 0, "failed": 0}
    if not connected():
        return counts
    now = now or datetime.now(timezone.utc)
    rows = (await session.execute(
        select(CrmOutbox)
        .where(CrmOutbox.status == PENDING, CrmOutbox.next_attempt_at <= now)
        .order_by(CrmOutbox.queued_at)
        .limit(limit))).scalars().all()
    waiting = set((await session.execute(
        select(CrmOutbox.customer_id).where(CrmOutbox.status == PENDING,
                                            CrmOutbox.next_attempt_at > now))).scalars())
    for row in rows:
        if row.customer_id in waiting:
            continue                 # an earlier event of theirs is waiting to be retried
        row.attempts += 1
        try:
            answer = await post_event(row.payload)
        except _Refused as exc:
            row.status, row.last_error, row.next_attempt_at = FAILED, str(exc)[:500], None
            counts["failed"] += 1
            log.error("dst_crm_event_refused", kind=row.event, outbox_id=str(row.id), error=str(exc))
        except _Retry as exc:
            row.last_error = str(exc)[:500]
            queued = row.queued_at if row.queued_at.tzinfo else row.queued_at.replace(tzinfo=timezone.utc)
            if now - queued > GIVE_UP_AFTER:
                row.status, row.next_attempt_at = FAILED, None
                counts["failed"] += 1
            else:
                row.next_attempt_at = now + _backoff(row.attempts)
                counts["retry"] += 1
            waiting.add(row.customer_id)
            log.warning("dst_crm_event_retry", kind=row.event, outbox_id=str(row.id),
                        attempts=row.attempts, error=str(exc))
        else:
            row.status, row.sent_at, row.last_error = SENT, now, None
            row.result = "; ".join(answer.get("applied") or [])[:500] or None
            counts["sent"] += 1
        await session.flush()
    if any(counts.values()):
        log.info("dst_crm_flushed", **counts)
    return counts


async def retry_failed(session: AsyncSession) -> int:
    """Put every refused event back in the queue (from the Settings page)."""
    rows = (await session.execute(select(CrmOutbox).where(CrmOutbox.status == FAILED))).scalars().all()
    now = datetime.now(timezone.utc)
    for row in rows:
        row.status, row.next_attempt_at, row.attempts = PENDING, now, 0
    await session.flush()
    return len(rows)


async def health() -> dict:
    """The CRM's own check: is its side ready. Never raises."""
    if not connected():
        return {"ok": False, "error": "Set DST_CRM_API_URL and DST_CRM_API_TOKEN first."}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(_base_url() + HEALTH_PATH, headers=_headers())
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"CRM not reachable: {exc.__class__.__name__}"}
    if response.status_code >= 300:
        return {"ok": False, "error": f"CRM answered {response.status_code}: {_message(response)}"}
    try:
        return {"ok": True, **response.json()}
    except ValueError:
        return {"ok": False, "error": "CRM answered something that is not JSON"}


@dataclass
class Status:
    connected: bool
    url: str
    pending: int
    failed: int
    sent: int
    last_sent_at: datetime | None
    last_error: str | None


async def status(session: AsyncSession) -> Status:
    """For the Settings page card."""
    counts = dict((await session.execute(
        select(CrmOutbox.status, func.count()).group_by(CrmOutbox.status))).all())
    last_sent = (await session.execute(select(func.max(CrmOutbox.sent_at)))).scalar()
    last_error = (await session.execute(
        select(CrmOutbox.last_error).where(CrmOutbox.last_error.is_not(None),
                                           CrmOutbox.status != SENT)
        .order_by(CrmOutbox.updated_at.desc()).limit(1))).scalar()
    return Status(connected=connected(), url=settings.crm_api_url or "",
                  pending=counts.get(PENDING, 0), failed=counts.get(FAILED, 0),
                  sent=counts.get(SENT, 0), last_sent_at=last_sent, last_error=last_error)
