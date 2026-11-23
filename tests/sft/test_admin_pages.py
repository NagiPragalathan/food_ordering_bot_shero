"""The SFT admin's sections beyond the journey: payments, customers, the
leads board, nudges by flow, offers, the bot's wording, the CSV, the dates."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from app.db.models import SftOffer
from app.sft import config, leads, nudges, payments
from app.sft import content as c
from tests.sft.conftest import NUMBER, say
from tests.sft.test_admin import admin  # noqa: F401 - the fixture


async def _enrolled(session, cfg, number=NUMBER, name="Priya"):
    await say(session, cfg, "SFT", number=number)
    await say(session, cfg, name, number=number)
    lead = await leads.get(session, number)
    await payments.mark_paid(session, lead, cfg, amount=Decimal("99"), currency="usd",
                             payment_intent="pi_123")
    return lead


async def test_payments_shows_what_was_paid_and_who_is_waiting(admin, session, cfg, rec):
    await _enrolled(session, cfg)
    await say(session, cfg, "SFT", number="15551230002")
    await say(session, cfg, "Meena", number="15551230002")
    await say(session, cfg, button=c.B_ENROL, title="Enroll now", number="15551230002")
    page = admin.get("/admin/sft?tab=customers").text
    assert "Paying customers" in page and "$99" in page and "Priya" in page
    assert "dashboard.stripe.com/payments/pi_123" in page and "data-sft-receipt-link" in page
    assert "Waiting to pay now" in page and "Meena" in page and "Revenue by day" in page


async def test_customers_lists_the_students_and_their_course_access(admin, session, cfg, rec):
    lead = await _enrolled(session, cfg)
    page = admin.get("/admin/sft?tab=customers").text
    assert "Priya" in page and "Team to send access" in page        # no course link is set
    assert "Priya" in admin.get("/admin/sft?tab=customers&access=help").text
    await leads.resolve(session, lead, "sent the access", by="Ops")
    assert "Not asked yet" in admin.get("/admin/sft?tab=customers").text
    lead.access_confirmed_at = leads.now()
    assert "Opened the course" in admin.get("/admin/sft?tab=customers&access=opened").text
    assert "Priya" not in admin.get("/admin/sft?tab=customers&access=help").text


async def test_the_leads_board_shows_each_stage_and_the_flow(admin, session, cfg, rec):
    await say(session, cfg, "SFT")
    await say(session, cfg, "Priya")
    page = admin.get("/admin/sft?tab=leads&layout=board").text
    assert "data-sft-board" in page
    for stage in c.FUNNEL:
        assert f'data-stage="{stage}"' in page
    assert "Waiting for a goal" in page and "Next: Follow-up 1" in page
    listing = admin.get("/admin/sft?tab=leads").text
    assert "Waiting for a goal" in listing


async def test_a_custom_date_range_filters_the_leads(admin, session, cfg, rec):
    await say(session, cfg, "SFT")
    lead = await leads.get(session, NUMBER)
    lead.created_at = leads.now() - timedelta(days=40)
    await session.flush()
    assert "Priya" not in admin.get("/admin/sft?tab=leads&range=7").text
    old = admin.get(f"/admin/sft?tab=leads&range=custom&from="
                    f"{(leads.now() - timedelta(days=45)).date()}&to="
                    f"{(leads.now() - timedelta(days=35)).date()}").text
    assert "+15551230001" in old
    # The sidebar keeps the chosen dates on the other dated pages.
    assert "tab=customers&amp;range=custom" in old or "tab=customers&range=custom" in old


async def test_nudges_show_who_is_in_which_flow_and_what_was_sent(admin, session, rec):
    cfg = await config.save(session, {"mode": "live", "send_from_hour": 0,
                                      "send_until_hour": 24}, "t")       # any hour
    await say(session, cfg, "SFT")
    await say(session, cfg, "Priya")
    lead = await leads.get(session, NUMBER)
    page = admin.get("/admin/sft?tab=nudges&section=flows").text
    assert "In the journey" in page and "Priya" in page
    await nudges.one(session, lead, cfg, leads.now() + timedelta(hours=25))
    sent = admin.get("/admin/sft?tab=nudges&section=sent").text
    assert "sft_followup_1" in sent and "Follow-up 1 of 3" in sent
    rules = admin.get("/admin/sft?tab=nudges").text
    assert "Follow-up 1" in rules and "Payment link again" in rules and "Hand to the team" in rules


async def test_offers_are_created_edited_switched_and_deleted(admin, session, cfg):
    made = admin.post("/admin/sft/offers", data={
        "title": "Launch week", "code": "sft 20", "kind": "amount", "value": "20",
        "active": "on", "sources": ["Instagram"]})
    assert made.status_code == 303
    offer = (await session.execute(select(SftOffer))).scalar_one()
    assert offer.code == "SFT20" and offer.value == Decimal("20") and offer.sources == ["Instagram"]
    page = admin.get("/admin/sft?tab=offers").text
    assert "SFT20" in page and "Running" in page and "$79" in page

    admin.post("/admin/sft/offers", data={"offer_id": str(offer.id), "title": "Launch week",
                                          "code": "SFT20", "kind": "percent", "value": "10",
                                          "active": "on"})
    assert offer.kind == "percent" and offer.sources == []
    admin.post(f"/admin/sft/offers/{offer.id}/toggle")
    assert not offer.active
    admin.post(f"/admin/sft/offers/{offer.id}/delete")
    assert (await session.execute(select(SftOffer))).scalar_one_or_none() is None


async def test_a_bad_offer_is_refused(admin, session, cfg):
    for data, why in (({"title": "", "value": "10"}, "name"),
                      ({"title": "x", "code": "SFT", "value": "10"}, "keywords"),
                      ({"title": "x", "kind": "percent", "value": "100"}, "less than 100"),
                      ({"title": "x", "kind": "price", "value": "120"}, "less than the fee"),
                      ({"title": "x", "value": "10", "starts_on": "2026-12-10",
                        "ends_on": "2026-12-01"}, "ends before")):
        answer = admin.post("/admin/sft/offers", data=data)
        assert why in answer.cookies.get("shero_flash", ""), why
    assert (await session.execute(select(SftOffer))).scalar_one_or_none() is None


async def test_the_bots_wording_is_edited_and_put_back(admin, session):
    page = admin.get("/admin/sft?tab=replies").text
    assert 'id="text-welcome"' in page and "What the bot says" in page
    admin.post("/admin/sft/texts", data={"key": "welcome", "text": "Hello {first}! {fee} only."})
    cfg = await config.load(session, fresh=True)
    assert cfg.texts["welcome"] == "Hello {first}! {fee} only."
    assert "Hello {first}! {fee} only." in admin.get("/admin/sft?tab=replies").text
    bad = admin.post("/admin/sft/texts", data={"key": "welcome", "text": "Hi {price}"})
    assert "can't be filled in" in bad.cookies.get("shero_flash", "")
    admin.post("/admin/sft/texts", data={"key": "welcome", "action": "reset"})
    assert "welcome" not in (await config.load(session, fresh=True)).texts


async def test_check_a_message_says_who_answers_it(admin, session, cfg):
    page = admin.get("/admin/sft?tab=replies&number=15551230009&message=I+saw+SFT").text
    assert "SFT answers it." in page and "keyword" in page
    page = admin.get("/admin/sft?tab=replies&number=15551230009&message=order+food").text
    assert "The ordering bot answers it." in page


async def test_the_csv_has_the_leads_and_the_payments(admin, session, cfg, rec):
    await _enrolled(session, cfg)
    csv = admin.get("/admin/sft/export?what=payments&range=all")
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv.headers["content-disposition"]
    lines = csv.text.strip().splitlines()
    assert lines[0].startswith("Name,WhatsApp") and "Priya" in lines[1] and "99" in lines[1]
    assert "+15551230001" in admin.get("/admin/sft/export?what=leads").text


async def test_bot_replies_looks_like_the_dashboards_and_saves_rows(admin, session):
    page = admin.get("/admin/sft?tab=replies").text
    for part in ("Who should SFT reply to?", "Whitelisted numbers", "Trigger keywords",
                 "Check a message", "id=\"sft-who-form\""):
        assert part in page, part
    # One row per number and per keyword, as the dashboard's page posts them.
    saved = admin.post("/admin/sft/replies", data={
        "mode": "test", "test_numbers": ["", "+1 555 123 0001", "1 555 123 0002"]})
    assert saved.status_code == 303
    cfg = await config.load(session, fresh=True)
    assert cfg.mode == "test" and cfg.test_numbers == ["15551230001", "15551230002"]
    admin.post("/admin/sft/replies", data={"keywords": ["sft", "food course"]})
    assert (await config.load(session, fresh=True)).keywords == ["sft", "food course"]
    assert "Test mode: replying only to 2 whitelisted numbers" in admin.get("/admin/sft?tab=replies").text


async def test_offers_have_a_list_with_cards_and_an_editor(admin, session, cfg):
    page = admin.get("/admin/sft?tab=offers").text
    assert "No offers yet" in page and "offer=new" in page
    editor = admin.get("/admin/sft?tab=offers&offer=new").text
    assert 'id="sft-offer-form"' in editor and "How the lead sees it" in editor
    admin.post("/admin/sft/offers", data={"title": "Launch", "code": "SFT20", "kind": "amount",
                                          "value": "20", "active": "on"})
    offer = (await session.execute(select(SftOffer))).scalar_one()
    page = admin.get("/admin/sft?tab=offers").text
    assert "data-copy=\"SFT20\"" in page and "Running" in page and "Lead pays $79" in page
    assert "Running offers" in page
    assert "SFT20" in admin.get("/admin/sft?tab=offers&status=running").text
    assert "No offers with that status" in admin.get("/admin/sft?tab=offers&status=ended").text
    edit = admin.get(f"/admin/sft?tab=offers&offer={offer.id}").text
    assert "Performance" in edit and 'value="SFT20"' in edit
    copied = admin.post(f"/admin/sft/offers/{offer.id}/duplicate")
    assert copied.status_code == 303
    rows = (await session.execute(select(SftOffer).order_by(SftOffer.created_at))).scalars().all()
    assert len(rows) == 2 and rows[1].code == "SFT202" and not rows[1].active


async def test_nudges_rules_look_like_the_dashboards(admin):
    page = admin.get("/admin/sft?tab=nudges").text
    for part in ("Nudges on", "Send between", "Wait time", "Trigger condition", "Save rules",
                 "Hand to the team"):
        assert part in page, part
    templates = admin.get("/admin/sft?tab=nudges&section=templates").text
    assert "Submit the missing ones to WhatsApp" in templates and "sft_followup_1" in templates


# --- The brochure PDF: replace it, go back to the original ----------------------------
PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


async def test_the_brochure_can_be_replaced_and_restored(admin, session, cfg, media_dir):
    page = admin.get("/admin/sft?tab=settings").text
    assert "data-sft-brochure" in page and "Replace PDF" in page and "Original" in page
    sent = admin.post("/admin/sft/brochure", data={"action": "upload"},
                      files={"brochure": ("SFT brochure v2.pdf", PDF, "application/pdf")})
    assert sent.status_code == 303
    cfg = await config.load(session, fresh=True)
    assert cfg.brochure_label == "SFT brochure v2.pdf"
    assert cfg.brochure_file.endswith("/Shero-Food-Training-SFT-Brochure.pdf")
    stored = media_dir / "sft" / cfg.brochure_file
    assert stored.read_bytes() == PDF
    assert cfg.brochure_link().endswith(f"/media/sft/{cfg.brochure_file}")
    page = admin.get("/admin/sft?tab=settings").text
    assert "SFT brochure v2.pdf" in page and "Use the original" in page

    # A second upload replaces the first and removes its file.
    admin.post("/admin/sft/brochure", data={"action": "upload"},
               files={"brochure": ("v3.pdf", PDF + b"% v3\n", "application/pdf")})
    newer = await config.load(session, fresh=True)
    assert newer.brochure_file != cfg.brochure_file and not stored.exists()

    admin.post("/admin/sft/brochure", data={"action": "reset"})
    back = await config.load(session, fresh=True)
    assert back.brochure_file == "" and back.brochure_link().endswith(
        "/static/sft/shero-food-training-brochure.pdf")


async def test_a_brochure_that_isnt_a_pdf_is_refused(admin, session, cfg):
    admin.post("/admin/sft/brochure", data={"action": "upload"},
               files={"brochure": ("photo.png", b"\x89PNG....", "image/png")})
    assert (await config.load(session, fresh=True)).brochure_file == ""
    admin.post("/admin/sft/brochure", data={"action": "upload"})
    assert (await config.load(session, fresh=True)).brochure_file == ""


async def test_the_bot_sends_the_uploaded_brochure(admin, session, cfg, rec):
    admin.post("/admin/sft/brochure", data={"action": "upload"},
               files={"brochure": ("b.pdf", PDF, "application/pdf")})
    cfg = await config.load(session, fresh=True)
    await say(session, cfg, "SFT")
    await say(session, cfg, "Priya")
    await say(session, cfg, button=c.B_BUSINESS, title="Start food business")
    await say(session, cfg, button=c.B_DETAILS, title="View course details")
    (kind, name, url), = [s for s in rec.sent if s[0] == "document"]
    assert url.endswith(f"/media/sft/{cfg.brochure_file}")


async def test_the_lead_page_links_the_receipt_and_shows_lead_status(admin, session, cfg, rec):
    lead = await _enrolled(session, cfg)
    page = admin.get(f"/admin/sft/leads/{lead.id}").text
    assert "data-sft-receipt-link" in page and "SFT-Enrollment-Receipt-" in page
    assert "SFT - Enrolled" in page and "data-sft-zoho-fields" in page


def test_a_document_goes_in_gallaboxs_shape():
    """path + mimeType: Gallabox accepts Meta's link/filename but never delivers it."""
    from app.integrations.gallabox import messages as m

    block = m.document_message("https://x.example/a/Brochure.pdf", filename="Brochure.pdf",
                               caption="The brochure")
    assert block == {"type": "document", "document": {
        "path": "https://x.example/a/Brochure.pdf", "mimeType": "application/pdf",
        "caption": "The brochure"}}


# --- Delete a lead: the bot forgets the number ----------------------------------------
async def test_deleting_a_lead_lets_the_number_start_over(admin, session, cfg, rec, media_dir):
    from app.db.models import SftEvent, SftLead

    lead = await _enrolled(session, cfg)               # paid (not a test) - needs the tick
    lead.test_payment = True                          # as a test payment, no tick needed
    lead_id, token = lead.id, lead.pay_token
    assert any((media_dir / "sft" / "receipts" / token).glob("*.pdf"))
    page = admin.get(f"/admin/sft/leads/{lead_id}").text
    assert "data-sft-forget" in page and "Delete this lead and start over" in page

    gone = admin.post(f"/admin/sft/leads/{lead_id}/action", data={"action": "forget"})
    assert gone.status_code == 303 and "tab=leads" in gone.headers["location"]
    assert await session.get(SftLead, lead_id) is None
    left = (await session.execute(select(SftEvent).where(SftEvent.lead_id == lead_id))).all()
    assert left == [] and not (media_dir / "sft" / "receipts" / token).exists()

    rec.clear()
    await say(session, cfg, "SFT")                     # a new lead: the name first
    assert rec.last()[1] == c.say(cfg, "ask_name")
    fresh = await leads.get(session, NUMBER)
    assert fresh.id != lead_id and fresh.paid_at is None


async def test_a_real_payment_needs_a_tick_to_delete(admin, session, cfg, rec):
    from app.db.models import SftLead

    lead = await _enrolled(session, cfg)
    page = admin.get(f"/admin/sft/leads/{lead.id}").text
    assert 'name="confirm_paid"' in page
    refused = admin.post(f"/admin/sft/leads/{lead.id}/action", data={"action": "forget"})
    assert "tick the box" in refused.cookies.get("shero_flash", "")
    assert await session.get(SftLead, lead.id) is not None
    admin.post(f"/admin/sft/leads/{lead.id}/action",
               data={"action": "forget", "confirm_paid": "on"})
    assert await session.get(SftLead, lead.id) is None


async def test_the_leads_list_has_a_delete_on_each_row(admin, session, cfg, rec):
    from app.db.models import SftLead

    await say(session, cfg, "SFT")
    await say(session, cfg, "Priya")
    lead = await leads.get(session, NUMBER)
    page = admin.get("/admin/sft?tab=leads").text
    assert "data-sft-row-delete" in page and "Yes, delete" in page
    assert f"/admin/sft/leads/{lead.id}/action" in page
    admin.post(f"/admin/sft/leads/{lead.id}/action", data={"action": "forget"})
    assert await session.get(SftLead, lead.id) is None
