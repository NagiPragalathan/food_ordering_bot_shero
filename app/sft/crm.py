"""SFT leads in Zoho CRM (Shero US's), kept apart from the food customers.

Synced by a job (sft_crm, every minute), never inside the chat: a slow or
broken Zoho cannot hold up a reply, and a failed push is retried with
backoff. One record per WhatsApp number, as for the ordering bot:

  * a number Zoho already has (a food customer's Lead or Contact) keeps its
    record and its Lead Source; it only gains the SFT tags and notes
  * a new number becomes a Lead with Lead Source "SFT - <source>"
    (SFT - WhatsApp, SFT - Meta Ad, SFT - Instagram, ...)

Tags tell SFT leads apart and say where each is, for Zoho's views and reports:

    SFT                          every SFT lead
    SFT <stage>                  exactly one: New, Contacted, Interested,
                                 Brochure Sent, Questions, Payment Pending,
                                 Enrolled, Nurture, Lost
    SFT Goal Business / Skills   what they want
    SFT via <source>             where they came from
    SFT With Team / Needs Call   a person has the chat / the team should call

Notes on the record carry the timeline (stages, their question, payments,
the team's actions), several events to one note. A team task (a drop-off)
becomes a Zoho Task on the record, due today, High priority when urgent.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import is_unset, settings
from app.core.exceptions import ConfigurationError, IntegrationError
from app.core.logging import get_logger
from app.db.models import SftEvent, SftLead
from app.integrations.zoho import crm as zcrm
from app.integrations.zoho import engagement
from app.integrations.zoho import fields as f
from app.integrations.zoho.client import API_VERSION, zoho_client
from app.sft import config, content as c
from app.sft.leads import now

log = get_logger(__name__)

BATCH = 25
BACKOFF_MINUTES = (2, 5, 15, 30, 60, 180, 360)
ERRORS = (IntegrationError, ConfigurationError, ValueError, KeyError)
SOURCE_PREFIX = "SFT - "
# Leads made in the admin's test chat never go to Zoho.
TEST_SOURCE = "Test chat"
TAG_MAX = 25
# Fields the CRM may not have (added by the ordering bot's setup, or a
# Lead Source option not created yet): dropped rather than failing the lead.
OPTIONAL = (f.LEAD_SOURCE, f.AD_ID, f.CAMPAIGN_ID, f.WHATSAPP_PROFILE_NAME, "Description")


def connected() -> bool:
    return not is_unset(settings.zoho_refresh_token) and not is_unset(settings.zoho_client_id)


def lead_source(lead: SftLead) -> str:
    return f"{SOURCE_PREFIX}{lead.source}"[:100]


def tags(lead: SftLead) -> set[str]:
    """The SFT tags the record should carry now."""
    wanted = {c.TAG_ALL, c.STAGE_TAGS.get(lead.stage, f"SFT {lead.stage}")}
    if lead.goal in c.GOAL_TAGS:
        wanted.add(c.GOAL_TAGS[lead.goal])
    if lead.source:
        wanted.add(f"SFT via {lead.source}"[:TAG_MAX])
    if lead.with_team:
        wanted.add(c.TAG_WITH_TEAM)
    if lead.needs_attention and not lead.with_team:
        wanted.add(c.TAG_NEEDS_CALL)
    return {t[:TAG_MAX] for t in wanted}


async def run(session: AsyncSession) -> int:
    """Push the leads that changed; returns how many were synced."""
    cfg = await config.load(session)
    if not cfg.zoho_sync or not connected():
        return 0
    due = (await session.execute(
        select(SftLead).where(SftLead.crm_dirty.is_(True), SftLead.source != TEST_SOURCE,
                              or_(SftLead.crm_next_try_at.is_(None),
                                  SftLead.crm_next_try_at <= now()))
        .order_by(SftLead.updated_at).limit(BATCH))).scalars().all()
    synced = 0
    for lead in due:
        try:
            await sync(session, lead, cfg)
            synced += 1
        except ERRORS as exc:
            lead.crm_attempts += 1
            wait = BACKOFF_MINUTES[min(lead.crm_attempts - 1, len(BACKOFF_MINUTES) - 1)]
            lead.crm_next_try_at = now() + timedelta(minutes=wait)
            lead.crm_error = str(exc)[:500]
            log.error("sft_zoho_sync_failed", lead_id=str(lead.id), error=str(exc),
                      retry_in_minutes=wait)
    return synced


async def sync(session: AsyncSession, lead: SftLead, cfg) -> None:
    module, record_id = await _record(lead)
    await _update(lead, module, record_id)
    await _tags(lead, module, record_id)
    await _notes(session, lead, module, record_id, tasks=cfg.zoho_tasks)
    lead.crm_dirty = False
    lead.crm_attempts = 0
    lead.crm_next_try_at = None
    lead.crm_synced_at = now()
    if lead.crm_error and not lead.crm_error.startswith("Lead Source"):
        lead.crm_error = None


async def _record(lead: SftLead) -> tuple[str, str]:
    """The lead's Zoho record: kept, found by number, or a new SFT Lead."""
    if lead.zoho_id and lead.zoho_module:
        return lead.zoho_module, lead.zoho_id
    for module in f.PERSON_MODULES:
        found = await zcrm.find_by_phone(module, lead.whatsapp_number)
        if found:
            lead.zoho_module, lead.zoho_id, lead.zoho_created = module, str(found["id"]), False
            log.info("sft_zoho_record_adopted", lead_id=str(lead.id), module=module)
            return module, lead.zoho_id
    first, last = f.split_name(lead.name)
    fields = {
        f.FIRST_NAME: first or None,
        f.LAST_NAME: last,
        f.PHONE: zcrm.phone(lead.whatsapp_number),
        f.EMAIL: lead.email,
        f.LEAD_SOURCE: lead_source(lead),
        f.AD_ID: lead.ad_id,
        f.CAMPAIGN_ID: lead.campaign_id,
        f.WHATSAPP_PROFILE_NAME: lead.profile_name,
        "Description": "Shero Food Training (SFT) lead from the WhatsApp course journey."
                       + (f" Source: {lead.source}" + (f" ({lead.source_detail})"
                                                       if lead.source_detail else "") + "."),
    }
    created = await _write_dropping(
        lambda payload: zoho_client.create_record(f.LEADS, payload), fields, lead)
    if not created:
        raise IntegrationError("zoho", "Zoho created the SFT lead but returned no id")
    lead.zoho_module, lead.zoho_id, lead.zoho_created = f.LEADS, str(created), True
    log.info("sft_zoho_lead_created", lead_id=str(lead.id))
    return f.LEADS, lead.zoho_id


async def _update(lead: SftLead, module: str, record_id: str) -> None:
    """Their name, email and Lead Source, on the bot's own SFT lead only: a
    record Zoho already had (a food customer's) keeps its own details."""
    if not lead.zoho_created or module != f.LEADS:
        return
    first, last = f.split_name(lead.name)
    fields = {f.EMAIL: lead.email, f.LEAD_SOURCE: lead_source(lead)}
    if lead.name:
        fields.update({f.FIRST_NAME: first or None, f.LAST_NAME: last})
    try:
        await _write_dropping(lambda payload: zoho_client.update_record(module, record_id, payload),
                              fields, lead)
    except IntegrationError as exc:
        if zcrm.is_missing_record(exc) or zcrm.is_converted_error(exc):
            # Deleted or converted in Zoho: find (or make) the record again next time.
            lead.zoho_id = lead.zoho_module = None
            lead.zoho_tags = []
        raise


async def _write_dropping(write, fields: dict, lead: SftLead):
    payload = f.compact(fields)
    for _ in range(len(OPTIONAL) + 1):
        try:
            return await write(payload)
        except IntegrationError as exc:
            refused = zcrm._rejected_field(exc)
            if refused not in OPTIONAL or refused not in payload:
                raise
            if refused == f.LEAD_SOURCE:
                lead.crm_error = (f"Lead Source '{payload[refused]}' is not an option in Zoho: "
                                  "use Prepare Zoho on the SFT page")
            log.warning("sft_zoho_field_dropped", field=refused)
            payload = {k: v for k, v in payload.items() if k != refused}
    raise AssertionError("unreachable")  # pragma: no cover


async def _tags(lead: SftLead, module: str, record_id: str) -> None:
    wanted = tags(lead)
    had = set(lead.zoho_tags or [])
    add, remove = wanted - had, {t for t in had - wanted if t.startswith("SFT")}
    path = f"/crm/{API_VERSION}/{module}/{record_id}/actions"
    if add:
        result = await zoho_client.post(f"{path}/add_tags",
                                        json={"tags": [{"name": t} for t in sorted(add)]})
        _check(result, "add tags")
    if remove:
        result = await zoho_client.post(f"{path}/remove_tags",
                                        json={"tags": [{"name": t} for t in sorted(remove)]})
        _check(result, "remove tags")
    lead.zoho_tags = sorted(wanted)


async def _notes(session: AsyncSession, lead: SftLead, module: str, record_id: str, *,
                 tasks: bool) -> None:
    pending = (await session.execute(
        select(SftEvent).where(SftEvent.lead_id == lead.id, SftEvent.crm_pending.is_(True))
        .order_by(SftEvent.at))).scalars().all()
    if not pending:
        return
    for row in pending:
        if tasks and row.kind == "attention" and (row.data or {}).get("task"):
            await _task(lead, module, record_id, row)
    title, content = note(lead, pending)
    await engagement.add_note(module, record_id, title=title, content=content)
    for row in pending:
        row.crm_pending = False


def note(lead: SftLead, events: list[SftEvent]) -> tuple[str, str]:
    """One note for the events since the last sync."""
    latest = events[-1]
    title = f"SFT: {c.STAGES.get(lead.stage, lead.stage)}"
    if latest.kind == "handover":
        title = "SFT: asked the team a question"
    elif latest.kind == "payment" and lead.paid_at:
        title = "SFT: enrolled (paid)"
    elif latest.kind == "attention":
        title = "SFT: needs the team"
    lines = [f"{_stamp(e.at)}  {e.detail}" for e in events if e.detail]
    lines += ["", f"Stage: {c.STAGES.get(lead.stage, lead.stage)}",
              f"Source: {lead.source}" + (f" ({lead.source_detail})" if lead.source_detail else "")]
    if lead.goal:
        lines.append(f"Goal: {c.GOALS.get(lead.goal, lead.goal)}")
    if lead.question and lead.with_team:
        lines.append(f'Their question: "{lead.question[:1000]}"')
    return title, "\n".join(lines)


async def _task(lead: SftLead, module: str, record_id: str, row: SftEvent) -> None:
    """The drop-off as a Zoho Task on the record (best effort: a note says it anyway)."""
    key = (row.data or {}).get("key")
    drop = c.DROPS.get(key)
    if drop is None:
        return
    first = c.first_name(lead.name or lead.profile_name)
    script = drop.script.format(first=first, goal=c.GOALS.get(lead.goal or "", "SFT").lower())
    body = {"data": [{
        "Subject": f"SFT: {drop.next_action} - {lead.name or lead.profile_name or 'lead'}"[:250],
        "Due_Date": date.today().isoformat(),
        "Priority": "High" if drop.priority == "high" else "Normal",
        "Status": "Not Started",
        "What_Id": {"id": record_id} if module == f.LEADS else None,
        "Who_Id": {"id": record_id} if module == f.CONTACTS else None,
        "$se_module": module if module == f.LEADS else None,
        "Description": (f"{row.detail}\n\nSuggested message:\n{script}\n\n"
                        f"WhatsApp: +{lead.whatsapp_number}\n"
                        "Reply in Gallabox, then close the task on the SFT admin page.")[:3000],
    }], "trigger": []}
    body["data"][0] = {k: v for k, v in body["data"][0].items() if v is not None}
    try:
        result = await zoho_client.post(f"/crm/{API_VERSION}/Tasks", json=body)
        _check(result, "task")
    except IntegrationError as exc:
        log.warning("sft_zoho_task_failed", lead_id=str(lead.id), error=str(exc))


def _check(result, what: str) -> None:
    for item in (result or {}).get("data") or []:
        if isinstance(item, dict) and str(item.get("status", "")).lower() == "error":
            raise IntegrationError("zoho", f"{what} refused: {item.get('code')} "
                                           f"{item.get('message')}", payload=item)


def _stamp(at: datetime) -> str:
    return at.strftime("%d %b %H:%M UTC")


async def prepare(session: AsyncSession) -> list[str]:
    """Add the SFT Lead Source options to Zoho's picklist (admin button).
    Returns the options added."""
    cfg = await config.load(session)
    wanted = [f"{SOURCE_PREFIX}{s}" for s in
              ("WhatsApp", "Meta Ad", *cfg.source_codes.values())]
    found = next((fld for fld in await zoho_client.list_fields(f.LEADS)
                  if fld.get("api_name") == f.LEAD_SOURCE), None)
    if found is None:
        raise IntegrationError("zoho", "Zoho's Leads module has no Lead Source field")
    have = {str(o.get("actual_value") or o.get("display_value"))
            for o in found.get("pick_list_values") or []}
    missing = [w for w in dict.fromkeys(wanted) if w not in have]
    if missing:
        await zoho_client.add_picklist_options(f.LEADS, str(found["id"]), missing)
    # Leads whose Lead Source was dropped: write it again.
    stale = (await session.execute(select(SftLead).where(
        SftLead.crm_error.like("Lead Source%")))).scalars().all()
    for lead in stale:
        lead.crm_error = None
        lead.crm_dirty = True
    return missing
