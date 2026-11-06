"""The nudges in Zoho, which the team works from.

* The stage. A customer in a bucket shows it as their Zoho stage: Bot Stage
  (and a Lead's Lead Status) reads No Response, Later, No or Dead Lead, as in
  KOB CRM, and goes back to their ordering stage when they order again. The
  bot adds those options to the picklists (`ensure`); no plan limits options.
* Zoho first. Before each send the bot reads the customer's stage in Zoho and
  follows what the team decided there (`follow`): Converted, a closed Lead
  Status (Junk, Lost, Not Qualified) or a bucket stops or moves the nudges.
* A timeline note for each message sent, button tapped and move.
* The nudge fields - Retargeting Bucket, Attempt Count, Last / Next
  Retargeting Date, Last Template ID - when the CRM has them.
* Nudge Messages: the bot's own module, one record per message, in a
  "Nudge Messages" list on the Lead and the Contact (`file_message`). Its
  delivery (sent, delivered, read, failed) and the button tapped are written
  back as WhatsApp reports them; when a Lead becomes a Contact its messages
  move to the Contact's list too (`relink`).

A plan at its limits refuses the fields and the module. `ensure` tries to
create them once a day (or at once, from the Nudges page's Try now), so they
appear by themselves after an upgrade. Until then each field Zoho refuses is
left out and the rest written, and the messages go to the notes only.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConfigurationError, IntegrationError
from app.core.logging import get_logger
from app.db.models import AppSetting, Customer, LeadStage, NudgeBucket, NudgeLead, NudgeSend
from app.integrations.zoho import crm, engagement, schema
from app.integrations.zoho import fields as f
from app.integrations.zoho.client import zoho_client

log = get_logger(__name__)

# Zoho's Retargeting Bucket values.
LABELS = {NudgeBucket.NO_RESPONSE: "No-response", NudgeBucket.LATER: "Later",
          NudgeBucket.NO: "No", NudgeBucket.DEAD: "Dead Lead"}
# The buckets as Zoho stages (Bot Stage, and a Lead's Lead Status).
STAGES = dict(zip((NudgeBucket.NO_RESPONSE, NudgeBucket.LATER, NudgeBucket.NO, NudgeBucket.DEAD),
                  f.NUDGE_STAGES))
BUCKET_OF = {label: bucket for bucket, label in STAGES.items()}
# A Lead Status the team sets to close a lead: no more nudges.
CLOSED = {"Junk Lead", "Lost Lead", "Not Qualified"}
FIELDS = (f.R_BUCKET, f.R_ATTEMPTS, f.R_LAST_DATE, f.R_NEXT_DATE, f.R_LAST_TEMPLATE)
SETUP_KEY = "NUDGE_ZOHO_SETUP"
SETUP_EVERY = timedelta(hours=24)

# The Nudge Messages module.
MODULE = f.NUDGE_MESSAGES
MODULE_FIELDS = tuple(s for s in schema.FIELDS if s.module == MODULE)
FIELD_BATCH = 5                                  # Zoho creates five fields per call
SEQUENCES = {"stage": "Stage message", NudgeBucket.NO_RESPONSE: "No Response",
             NudgeBucket.LATER: "Later", NudgeBucket.NO: "No"}
DELIVERY = {"sent": "Sent", "delivered": "Delivered", "read": "Read", "failed": "Failed"}


def record(customer: Customer) -> tuple[str, str] | None:
    if customer.zoho_contact_id:
        return f.CONTACTS, customer.zoho_contact_id
    if customer.zoho_lead_id:
        return f.LEADS, customer.zoho_lead_id
    return None


def zoho_stage(customer: Customer, lead: NudgeLead) -> str:
    """Their stage as Zoho shows it: the bucket, or where they are in ordering."""
    return STAGES.get(lead.bucket or "") or customer.lead_stage


def values(module: str, customer: Customer, lead: NudgeLead, template: str | None = None, *,
           stage: bool = True) -> dict:
    data: dict = {}
    if stage:
        data[f.BOT_STAGE] = zoho_stage(customer, lead)
        if module == f.LEADS:
            data[f.LEAD_STATUS] = data[f.BOT_STAGE]
    data.update({f.R_BUCKET: LABELS.get(lead.bucket or "", "Engaged"),
                 f.R_ATTEMPTS: lead.attempts,
                 f.R_LAST_DATE: f.zoho_datetime(lead.last_sent_at),
                 f.R_NEXT_DATE: f.zoho_datetime(lead.next_due_at)})
    if template:
        data[f.R_LAST_TEMPLATE] = template[:40]
    return data


async def sync(customer: Customer, lead: NudgeLead, lines: list[str], *,
               template: str | None = None, stage: bool = True) -> None:
    """Write the stage and fields, and one note with `lines`. `stage=False`
    leaves the stage as the team set it. Never raises: a Zoho problem is kept
    on the lead for the page."""
    where = record(customer)
    if where is None:
        return
    module, record_id = where
    try:
        if lines:
            await engagement.add_note(module, record_id, title="WhatsApp nudges",
                                      content="\n".join(lines))
        payload = values(module, customer, lead, template, stage=stage)
        for _ in range(len(payload) + 1):
            try:
                await crm.update_record(module, record_id, payload)
                break
            except IntegrationError as exc:
                field = crm._rejected_field(exc)
                if not field or field not in payload:
                    raise
                payload.pop(field)     # a field (or option) this CRM lacks: write the rest
                if not payload:
                    break
        lead.zoho_error = None
    except (IntegrationError, ConfigurationError) as exc:
        lead.zoho_error = str(exc)[:300]
        log.warning("nudge_zoho_failed", customer_id=str(customer.id), error=str(exc))


# --- Nudge Messages: one record per message -------------------------------------------
async def _write(write, payload: dict) -> str | None:
    """`write(payload)`, leaving out each field Zoho refuses (one the CRM has
    not got yet, say) and writing the rest."""
    payload = dict(payload)
    for _ in range(len(payload) + 1):
        try:
            return await write(payload)
        except IntegrationError as exc:
            field = crm._rejected_field(exc)
            if not field or field not in payload or field in (f.N_LEAD, f.N_CONTACT):
                raise
            payload.pop(field)
    return None


def _title(send: NudgeSend) -> str:
    if send.kind == "stage":
        return f"{send.stage or 'Stage'} - stage message"[:120]
    return f"{SEQUENCES.get(send.kind, send.kind)} - message {send.attempt}"[:120]


def message_values(customer: Customer, send: NudgeSend) -> dict | None:
    """The record for one nudge, under the customer's Lead or Contact."""
    where = record(customer)
    if where is None:
        return None
    module, record_id = where
    data = {
        f.N_NAME: _title(send),
        (f.N_CONTACT if module == f.CONTACTS else f.N_LEAD): {"id": record_id},
        f.N_STAGE: send.stage,
        f.N_SEQUENCE: SEQUENCES.get(send.kind),
        f.N_TEMPLATE: send.template,
        f.N_ATTEMPT: send.attempt,
        f.N_SEND_STATUS: "Sent" if send.status == "sent" else "Failed",
        f.N_SEND_ERROR: (send.error or "")[:255] or None,
        f.N_SENT_TIME: f.zoho_datetime(send.sent_at),
        f.N_WHATSAPP_ID: send.provider_message_id,
    }
    return {key: value for key, value in data.items() if value is not None}


async def file_message(customer: Customer, send: NudgeSend) -> None:
    """File one nudge in Nudge Messages. Never raises: a failure is logged
    and the message stays in the timeline note."""
    data = message_values(customer, send)
    if data is None:
        return
    try:
        send.zoho_id = await _write(lambda payload: zoho_client.create_record(MODULE, payload),
                                    data)
    except (IntegrationError, ConfigurationError) as exc:
        log.warning("nudge_message_zoho_failed", customer_id=str(customer.id), error=str(exc))


async def update_message(send: NudgeSend, data: dict) -> None:
    """Change a filed message's record. Never raises."""
    if not send.zoho_id:
        return
    try:
        await _write(lambda payload: zoho_client.update_record(MODULE, send.zoho_id, payload),
                     data)
    except (IntegrationError, ConfigurationError) as exc:
        log.warning("nudge_message_zoho_update_failed", zoho_id=send.zoho_id, error=str(exc))


async def message_delivered(send: NudgeSend) -> None:
    """WhatsApp's latest word on it: sent, delivered, read or failed."""
    status = DELIVERY.get(send.delivery_status or "")
    if status:
        await update_message(send, {f.N_DELIVERY: status,
                                    f.N_DELIVERY_TIME: f.zoho_datetime(send.delivery_at)})


async def message_replied(send: NudgeSend) -> None:
    """The button they tapped on it."""
    if send.reply:
        await update_message(send, {f.N_REPLY: send.reply,
                                    f.N_REPLY_TIME: f.zoho_datetime(send.replied_at)})


async def relink(session: AsyncSession, customer: Customer) -> None:
    """A Lead became a Contact: its messages join the Contact's list too
    (the Lead keeps them, so the history reads the same from either)."""
    if not customer.zoho_contact_id:
        return
    sends = (await session.execute(select(NudgeSend).where(
        NudgeSend.customer_id == customer.id, NudgeSend.zoho_id.is_not(None)))).scalars()
    for send in sends:
        await update_message(send, {f.N_CONTACT: {"id": customer.zoho_contact_id}})


async def follow(customer: Customer, lead: NudgeLead, now: datetime) -> str | None:
    """Before a send: what Zoho says. None to send; otherwise why not, with the
    lead moved to what the team decided in Zoho.

    Ordering stages are the bot's to set, so a different one in Zoho means an
    update that didn't land - the send goes and its write corrects Zoho. The
    team's decisions are Converted, a closed Lead Status and the buckets."""
    where = record(customer)
    if where is None or lead.zoho_error:
        return None          # no record, or our last write failed: Zoho is behind, not the team
    module, record_id = where
    try:
        row = await crm.get_record(module, record_id)
    except ConfigurationError:
        return None          # Zoho isn't set up here
    except IntegrationError as exc:
        return f"Couldn't read the stage in Zoho, will try again: {exc}"[:200]
    if not row:
        return None
    status, stage = str(row.get(f.LEAD_STATUS) or ""), str(row.get(f.BOT_STAGE) or "")
    if status in CLOSED or (stage == LeadStage.CONVERTED and customer.lead_stage != LeadStage.CONVERTED):
        _move(lead, NudgeBucket.DEAD, now)
        return f"Zoho shows {status if status in CLOSED else stage}: no more nudges"
    bucket = BUCKET_OF.get(stage)
    if bucket and bucket != lead.bucket:
        _move(lead, bucket, now)
        return f"Zoho shows {stage}: following Zoho"
    return None


def _move(lead: NudgeLead, bucket: str, now: datetime) -> None:
    lead.bucket_from = lead.bucket_from if lead.bucket else lead.stage
    lead.bucket, lead.bucket_at = bucket, now
    lead.attempts = lead.failures = 0
    lead.next_due_at = None


# --- setting Zoho up for the nudges -------------------------------------------------------
async def setup_status(session: AsyncSession) -> dict:
    row = (await session.execute(select(AppSetting).where(AppSetting.key == SETUP_KEY))).scalar_one_or_none()
    try:
        return json.loads(row.value) if row and row.value else {}
    except ValueError:
        return {}


def _why(exc: Exception, what: str = "custom field") -> str:
    """Zoho's refusal, in words the Nudges page can show."""
    text = str(exc)
    if "LIMIT_EXCEEDED" in text:
        return f"Zoho's {what} limit is reached on this plan"
    if "NOT_SUPPORTED" in text:
        return "Custom modules are not available on this Zoho plan"
    if "NO_PERMISSION" in text or "OAUTH_SCOPE" in text:
        return "The Zoho connection may not change modules and fields (reconnect Zoho as an admin)"
    return text[:300]


async def _ensure_module(result: dict) -> bool:
    """The Nudge Messages module and its fields. True once records can be
    filed: the module is there with its Lead and Contact lookups."""
    if await zoho_client.get_module(MODULE) is None:
        labels = schema.MODULES[MODULE]
        try:
            await zoho_client.create_module(MODULE, labels["singular_label"],
                                            labels["plural_label"])
        except IntegrationError as exc:
            result["module_error"] = _why(exc, "custom module")
            return False
        result["module_created"] = True
    have = {r.get("api_name") for r in await zoho_client.list_fields(MODULE)}
    specs = [s for s in MODULE_FIELDS if s.api_name not in have]
    for start in range(0, len(specs), FIELD_BATCH):
        try:
            await zoho_client.create_fields(MODULE, [s.body for s in specs[start:start + FIELD_BATCH]])
        except IntegrationError as exc:
            result["error"] = result["error"] or _why(exc)
    if specs:
        have = {r.get("api_name") for r in await zoho_client.list_fields(MODULE)}
    result["fields_missing"] += [f"{MODULE}.{s.api_name}" for s in MODULE_FIELDS
                                 if s.api_name not in have]
    return f.N_LEAD in have and f.N_CONTACT in have


async def ensure(session: AsyncSession, *, force: bool = False) -> dict:
    """Add the bucket stages to Zoho's stage picklists, and try to create the
    nudge fields and the Nudge Messages module. At most once a day unless
    forced; never raises."""
    status = await setup_status(session)
    now = datetime.now(timezone.utc)
    try:
        last = datetime.fromisoformat(status["checked_at"]) if status.get("checked_at") else None
    except (TypeError, ValueError):
        last = None
    if not force and last and now - last < SETUP_EVERY:
        return status

    result: dict = {"checked_at": now.isoformat(), "stages_added": [], "fields_missing": [],
                    "error": None, "module_ready": False, "module_error": None}
    try:
        for module in (f.LEADS, f.CONTACTS):
            rows = {r.get("api_name"): r for r in await zoho_client.list_fields(module)}
            for name in (f.BOT_STAGE, f.LEAD_STATUS) if module == f.LEADS else (f.BOT_STAGE,):
                row = rows.get(name)
                have = {o.get("display_value") for o in (row or {}).get("pick_list_values") or []}
                add = [label for label in f.NUDGE_STAGES if label not in have]
                if row and add:
                    await zoho_client.add_picklist_options(module, row["id"], add)
                    result["stages_added"] += [f"{module}.{name}: {label}" for label in add]
            specs = [s for s in schema.FIELDS
                     if s.module == module and s.api_name in FIELDS and s.api_name not in rows]
            if specs:
                try:
                    await zoho_client.create_fields(module, [s.body for s in specs])
                except IntegrationError as exc:
                    result["error"] = _why(exc)
                rows = {r.get("api_name") for r in await zoho_client.list_fields(module)}
                result["fields_missing"] += [f"{module}.{s.api_name}" for s in specs
                                             if s.api_name not in rows]
    except ConfigurationError:
        result["error"] = "Zoho is not set up"
    except IntegrationError as exc:
        result["error"] = str(exc)[:300]
    try:
        result["module_ready"] = await _ensure_module(result)
    except ConfigurationError:
        result["module_error"] = "Zoho is not set up"
    except IntegrationError as exc:
        result["module_error"] = _why(exc, "custom module")
    log.info("nudge_zoho_setup", added=len(result["stages_added"]),
             fields_missing=len(result["fields_missing"]), module_ready=result["module_ready"],
             error=result["error"] or result["module_error"])

    row = (await session.execute(select(AppSetting).where(AppSetting.key == SETUP_KEY))).scalar_one_or_none()
    if row is None:
        row = AppSetting(key=SETUP_KEY, is_secret=False)
        session.add(row)
    row.value, row.updated_by = json.dumps(result), "nudges"
    await session.flush()
    return result
