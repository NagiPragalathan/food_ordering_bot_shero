"""SFT leads in Zoho: Lead Source, tags, notes and tasks (app/sft/crm.py)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.exceptions import IntegrationError
from app.db.models import SftEvent
from app.integrations.zoho import crm as zcrm
from app.sft import content as c
from app.sft import crm, leads
from tests.sft.conftest import NUMBER, say


class FakeZoho:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict]] = []
        self.updated: list[tuple[str, str, dict]] = []
        self.posts: list[tuple[str, dict]] = []
        self.refuse_source = False

    async def create_record(self, module, fields):
        if self.refuse_source and "Lead_Source" in fields:
            raise IntegrationError("zoho", "invalid", payload={
                "code": "INVALID_DATA", "details": {"api_name": "Lead_Source"}})
        self.created.append((module, fields))
        return "5001"

    async def update_record(self, module, record_id, fields):
        self.updated.append((module, record_id, fields))
        return record_id

    async def post(self, path, json=None, **kw):
        self.posts.append((path, json))
        return {"data": [{"status": "success", "details": {"id": "n1"}}]}

    def paths(self, part: str) -> list[dict]:
        return [body for path, body in self.posts if part in path]


@pytest.fixture
def zoho(monkeypatch):
    fake = FakeZoho()
    from app.integrations.zoho import client

    monkeypatch.setattr(client, "zoho_client", fake)
    monkeypatch.setattr(crm, "zoho_client", fake)
    monkeypatch.setattr("app.integrations.zoho.engagement.zoho_client", fake)
    monkeypatch.setattr(crm, "connected", lambda: True)

    async def nobody(module, number):
        return None

    monkeypatch.setattr(zcrm, "find_by_phone", nobody)
    return fake


async def _brochure_lead(session, cfg, source_text="SFT"):
    await say(session, cfg, source_text)
    await say(session, cfg, "Priya Raman")
    await say(session, cfg, button=c.B_BUSINESS, title="Start food business")
    await say(session, cfg, button=c.B_DETAILS, title="View course details")
    return await leads.get(session, NUMBER)


async def test_a_new_number_becomes_an_sft_lead_with_its_source(session, cfg, rec, zoho):
    lead = await _brochure_lead(session, cfg, "SFT web")
    assert await crm.run(session) == 1
    module, fields = zoho.created[0]
    assert module == "Leads" and fields["Lead_Source"] == "SFT - Website"
    assert fields["First_Name"] == "Priya" and fields["Last_Name"] == "Raman"
    assert fields["Phone"] == f"+{NUMBER}"
    assert lead.zoho_id == "5001" and lead.zoho_created and not lead.crm_dirty


async def test_tags_tell_sft_leads_apart(session, cfg, rec, zoho):
    lead = await _brochure_lead(session, cfg)
    await crm.run(session)
    added = {t["name"] for body in zoho.paths("/add_tags") for t in body["tags"]}
    assert added == {"SFT", "SFT Brochure Sent", "SFT Goal Business", "SFT via WhatsApp"}
    assert all(len(t) <= 25 for t in added)

    await say(session, cfg, "yes")                       # now Payment Pending
    zoho.posts.clear()
    await crm.run(session)
    assert [t["name"] for b in zoho.paths("/add_tags") for t in b["tags"]] == ["SFT Payment Pending"]
    assert [t["name"] for b in zoho.paths("/remove_tags") for t in b["tags"]] == ["SFT Brochure Sent"]
    assert "SFT Payment Pending" in lead.zoho_tags


async def test_the_timeline_goes_as_one_note(session, cfg, rec, zoho):
    await _brochure_lead(session, cfg)
    await crm.run(session)
    notes = zoho.paths("/Notes")
    assert len(notes) == 1
    note = notes[0]["data"][0]
    assert note["Note_Title"] == "SFT: Brochure Sent"
    assert "Interested → Brochure Sent" in note["Note_Content"]
    assert "Goal: Start a food business" in note["Note_Content"]
    pending = (await session.execute(select(SftEvent).where(SftEvent.crm_pending.is_(True)))
               ).scalars().all()
    assert pending == []


async def test_a_question_for_the_team_becomes_a_high_priority_task(session, cfg, rec, zoho):
    await _brochure_lead(session, cfg)
    await say(session, cfg, button=c.B_ASK, title="Ask a question")
    await say(session, cfg, "Can I join from Toronto?")
    await crm.run(session)
    task = zoho.paths("/Tasks")[0]["data"][0]
    assert task["Priority"] == "High" and task["$se_module"] == "Leads"
    assert task["What_Id"] == {"id": "5001"} and "Answer, then hand back" in task["Subject"]
    assert "Toronto" in task["Description"]
    added = {t["name"] for body in zoho.paths("/add_tags") for t in body["tags"]}
    assert "SFT With Team" in added
    note = zoho.paths("/Notes")[-1]["data"][0]
    assert note["Note_Title"] == "SFT: needs the team" or "question" in note["Note_Title"]


async def test_an_existing_zoho_record_keeps_its_own_details(session, cfg, rec, zoho,
                                                            monkeypatch):
    async def found(module, number):
        return {"id": "7007"} if module == "Contacts" or module == "Leads" else None

    monkeypatch.setattr(zcrm, "find_by_phone", found)
    lead = await _brochure_lead(session, cfg)
    await crm.run(session)
    assert zoho.created == [] and zoho.updated == []          # name and Lead Source untouched
    assert lead.zoho_id == "7007" and not lead.zoho_created
    assert zoho.paths("/7007/actions/add_tags")


async def test_a_missing_lead_source_option_is_dropped_not_fatal(session, cfg, rec, zoho):
    zoho.refuse_source = True
    lead = await _brochure_lead(session, cfg)
    await crm.run(session)
    assert lead.zoho_id == "5001" and "Lead_Source" not in zoho.created[0][1]
    assert lead.crm_error.startswith("Lead Source")


async def test_zoho_down_is_retried_later(session, cfg, rec, zoho, monkeypatch):
    async def down(*a, **k):
        raise IntegrationError("zoho", "503")

    monkeypatch.setattr(zoho, "create_record", down)
    lead = await _brochure_lead(session, cfg)
    assert await crm.run(session) == 0
    assert lead.crm_dirty and lead.crm_attempts == 1 and lead.crm_next_try_at is not None
    assert await crm.run(session) == 0                        # not due again yet


async def test_nothing_goes_when_zoho_is_not_connected(session, cfg, rec):
    await _brochure_lead(session, cfg)
    assert await crm.run(session) == 0


async def test_test_chat_leads_never_go_to_zoho(session, cfg, rec, zoho):
    lead = await _brochure_lead(session, cfg)
    lead.source = "Test chat"
    assert await crm.run(session) == 0 and zoho.created == []


async def test_prepare_adds_the_sft_lead_sources(session, cfg, zoho, monkeypatch):
    added = []

    async def fields(module):
        return [{"api_name": "Lead_Source", "id": "f1",
                 "pick_list_values": [{"actual_value": "SFT - WhatsApp"}]}]

    async def add(module, field_id, options):
        added.extend(options)

    monkeypatch.setattr(zoho, "list_fields", fields, raising=False)
    monkeypatch.setattr(zoho, "add_picklist_options", add, raising=False)
    missing = await crm.prepare(session)
    assert "SFT - WhatsApp" not in missing and "SFT - Meta Ad" in missing
    assert "SFT - Instagram" in added
