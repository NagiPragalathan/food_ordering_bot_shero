"""The nudges in Zoho: the customer's bucket and messages on their Lead or
Contact, and a timeline note for each message sent, button tapped or move.

A CRM without the fields still gets the notes: each field Zoho refuses is
left out and the rest written (scripts/setup_zoho_crm.py creates them).
"""

from __future__ import annotations

from app.core.exceptions import ConfigurationError, IntegrationError
from app.core.logging import get_logger
from app.db.models import Customer, NudgeBucket, NudgeLead
from app.integrations.zoho import crm, engagement
from app.integrations.zoho import fields as f

log = get_logger(__name__)

LABELS = {NudgeBucket.NO_RESPONSE: "No-response", NudgeBucket.LATER: "Later",
          NudgeBucket.NO: "No", NudgeBucket.DEAD: "Dead Lead"}


def record(customer: Customer) -> tuple[str, str] | None:
    if customer.zoho_contact_id:
        return f.CONTACTS, customer.zoho_contact_id
    if customer.zoho_lead_id:
        return f.LEADS, customer.zoho_lead_id
    return None


def values(lead: NudgeLead, template: str | None = None) -> dict:
    data = {f.R_BUCKET: LABELS.get(lead.bucket or "", "Engaged"),
            f.R_ATTEMPTS: lead.attempts,
            f.R_LAST_DATE: f.zoho_datetime(lead.last_sent_at),
            f.R_NEXT_DATE: f.zoho_datetime(lead.next_due_at)}
    if template:
        data[f.R_LAST_TEMPLATE] = template[:40]
    return data


async def sync(customer: Customer, lead: NudgeLead, lines: list[str], *,
               template: str | None = None) -> None:
    """Write the fields and one note with `lines`. Never raises: a Zoho
    problem is kept on the lead for the page."""
    where = record(customer)
    if where is None:
        return
    module, record_id = where
    try:
        if lines:
            await engagement.add_note(module, record_id, title="WhatsApp nudges",
                                      content="\n".join(lines))
        payload = values(lead, template)
        for _ in range(len(payload) + 1):
            try:
                await crm.update_record(module, record_id, payload)
                break
            except IntegrationError as exc:
                field = crm._rejected_field(exc)
                if not field or field not in payload:
                    raise
                payload.pop(field)     # a field this CRM does not have: write the rest
                if not payload:
                    break
        lead.zoho_error = None
    except (IntegrationError, ConfigurationError) as exc:
        lead.zoho_error = str(exc)[:300]
        log.warning("nudge_zoho_failed", customer_id=str(customer.id), error=str(exc))
