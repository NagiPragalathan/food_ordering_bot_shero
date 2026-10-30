"""The SMO retargeting fields on a customer's Zoho record (handoff section 8).

A follow-up journey lives in this database (nudge_journeys): one scheduler
decides every send. Its state is mirrored onto the customer's Lead or
Contact as the handoff's fields - bucket, status, counters, dates, last
template, keyword, flow status, last order, Dead Lead reason - each time it
moves: a send, a reply, ORDER FOOD, an abandoned flow, an order, the end,
a hold, an opt-out, a change by the team.

The fields are created by `python -m scripts.setup_zoho_crm --apply`. A CRM
without some of them is fine: Zoho refuses an unknown field, and that field
is left out and the rest written. Best effort throughout, like every nudge
write to Zoho - Zoho being down never stops a follow-up.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConfigurationError, IntegrationError
from app.core.logging import get_logger
from app.db.models import Customer, JourneyStatus, NudgeJourney, Order, OrderStage, PaymentStatus
from app.integrations.zoho import crm
from app.integrations.zoho import fields as f
from app.services.nudges import store
from app.services.nudges.audience import digits

log = get_logger(__name__)

HISTORY_KEPT = 10
NUDGE_HOLDS = {"complaint": "Complaint", "support": "Support issue",
               "invalid_contact": "Invalid contact", "other": "Other hold"}


def suppression(customer: Customer) -> str:
    if customer.nudges_opted_out_at is not None:
        return "Opted out"
    if customer.nudge_hold:
        return NUDGE_HOLDS.get(customer.nudge_hold, "Other hold")
    return "None"


def _history(journey: NudgeJourney) -> str:
    lines = []
    for entry in list(journey.recent or [])[-HISTORY_KEPT:]:
        try:
            at = datetime.fromisoformat(str(entry.get("at")))
            lines.append(f"{entry.get('code')} {at:%Y-%m-%d %H:%M} UTC")
        except ValueError:
            lines.append(str(entry.get("code")))
    return "\n".join(lines)


def values(customer: Customer, journey: NudgeJourney | None, last_order: Order | None) -> dict:
    """The fields, as Zoho wants them."""
    from app.services.nudges import followup

    data = {
        f.R_CONTACT_KEY: digits(customer.whatsapp_number),
        f.R_ENTRY_VERTICAL: "SMO",
        f.R_OPT_IN: customer.nudges_opted_out_at is None,
        f.R_OPT_IN_SOURCE: customer.lead_source or "WhatsApp",
        f.R_OPT_IN_TIME: f.zoho_datetime(customer.nudges_opted_out_at or customer.created_at),
        f.R_SUPPRESSION: suppression(customer),
        f.R_STATUS: followup.crm_status(journey, customer),
        f.R_LAST_ORDER_DATE: f.zoho_datetime(
            (last_order.paid_at or last_order.created_at) if last_order else None),
        f.R_LAST_ORDER_STATUS: (last_order.stage if last_order else None),
    }
    if journey is not None:
        running = journey.status in (JourneyStatus.ACTIVE, JourneyStatus.PAUSED)
        bucket = followup.crm_bucket(journey)
        data.update({
            f.R_BUCKET: bucket,
            f.R_START: f.zoho_datetime(journey.started_at),
            f.R_ATTEMPTS: journey.attempts,
            f.R_BUCKET_ATTEMPTS: journey.bucket_attempts,
            f.R_LAST_DATE: f.zoho_datetime(journey.last_sent_at),
            f.R_NEXT_DATE: f.zoho_datetime(journey.next_due_at) if running else None,
            f.R_ACTIVE_JOURNEY: "SMO" if running else "None",
            f.R_LAST_TEMPLATE: journey.last_message_code,
            f.R_TEMPLATE_HISTORY: _history(journey) or None,
            f.R_TIMEZONE: (f"{journey.timezone} ({journey.timezone_source})"
                           if journey.timezone else None),
            f.R_LAST_KEYWORD: journey.last_keyword,
            f.R_FLOW_STATUS: followup.FLOW_LABELS.get(journey.flow_status, "Not started"),
            f.R_DEAD_REASON: (followup.END_REASONS.get(journey.end_reason or "", journey.end_reason)
                              if bucket == "Dead Lead" else None),
        })
    return data


async def _last_order(session: AsyncSession, customer: Customer) -> Order | None:
    return (await session.execute(
        select(Order).where(Order.customer_id == customer.id,
                            Order.payment_status.in_([PaymentStatus.PAID, PaymentStatus.REFUNDED])
                            | (Order.stage == OrderStage.CANCELLED))
        .order_by(Order.created_at.desc()).limit(1))).scalar_one_or_none()


async def _latest_journey(session: AsyncSession, customer: Customer) -> NudgeJourney | None:
    return (await session.execute(
        select(NudgeJourney).where(NudgeJourney.customer_id == customer.id)
        .order_by(NudgeJourney.updated_at.desc()).limit(1))).scalar_one_or_none()


async def sync(session: AsyncSession, customer: Customer, journey: NudgeJourney | None = None,
               *, find_journey: bool = False) -> bool:
    """Write the fields to their Zoho record. True if Zoho took them."""
    from app.services.nudges.runner import zoho_record

    _, settings = await store.load_settings(session)
    record = zoho_record(customer)
    if not settings.zoho_sync or record is None:
        return False
    if journey is None and find_journey:
        journey = await _latest_journey(session, customer)
    payload = values(customer, journey, await _last_order(session, customer))
    module, record_id = record
    dropped = []
    try:
        for _ in range(len(payload) + 1):
            try:
                await crm.update_record(module, record_id, payload)
                break
            except IntegrationError as exc:
                field = crm._rejected_field(exc)
                if not field or field not in payload:
                    raise
                # A field this CRM does not have yet: write the rest.
                dropped.append(field)
                payload.pop(field)
        if journey is not None:
            journey.zoho_synced_at = datetime.now(timezone.utc)
            journey.zoho_error = (f"Not in this Zoho yet: {', '.join(dropped)}"[:300]
                                  if dropped else None)
        return True
    except (IntegrationError, ConfigurationError) as exc:
        if journey is not None:
            journey.zoho_error = str(exc)[:300]
        log.warning("retargeting_fields_sync_failed", customer_id=str(customer.id), error=str(exc))
        return False
