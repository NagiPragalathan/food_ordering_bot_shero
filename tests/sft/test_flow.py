"""The SFT journey on WhatsApp, as the team's notes asked for it:

  * the welcome gets their name (asked first)
  * the lead-entry message has no "Course details" button any more
  * View course details sends the brochure, then Yes / Not right now / Ask a question
  * Yes -> payment; Not right now -> follow-ups; Ask a question -> they type it,
    "we're transferring you", and the bot says nothing more until the team
    hands the chat back
"""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import SftEvent, SftLead
from app.sft import content as c
from app.sft import leads
from tests.sft.conftest import NUMBER, say


async def _lead(session) -> SftLead:
    return await leads.get(session, NUMBER)


async def _to_brochure(session, cfg, rec) -> SftLead:
    await say(session, cfg, "SFT")
    await say(session, cfg, "Priya")
    await say(session, cfg, button=c.B_BUSINESS, title="Start food business")
    await say(session, cfg, button=c.B_DETAILS, title="View course details")
    return await _lead(session)


async def test_a_new_lead_is_asked_their_name_then_welcomed_by_it(session, cfg, rec):
    assert await say(session, cfg, "SFT") == "asked name"
    kind, body, _ = rec.last()
    assert kind == "text" and "may I know your name" in body

    assert await say(session, cfg, "priya") == "welcome"
    kind, body, buttons = rec.last()
    assert kind == "buttons" and body.startswith("Hi Priya! 👋")
    assert "$99" in body and "10 short videos" in body
    lead = await _lead(session)
    assert lead.name == "Priya" and lead.stage == c.CONTACTED and lead.step == c.GOAL


async def test_the_lead_entry_message_has_no_course_details_button(session, cfg, rec):
    await say(session, cfg, "SFT")
    await say(session, cfg, "Priya")
    assert rec.buttons() == ["Start food business", "Improve my skills"]
    assert all("Course details" not in t for t in rec.buttons())


async def test_a_keyword_or_question_is_not_taken_as_a_name(session, cfg, rec):
    await say(session, cfg, "SFT")
    rec.clear()
    await say(session, cfg, "sft")
    assert "may I know your name" in rec.last()[1]
    await say(session, cfg, "how much is the course?")
    assert rec.last()[1] == c.ASK_NAME_AGAIN
    assert (await _lead(session)).name is None


async def test_without_asking_the_whatsapp_name_is_used(session, rec):
    from app.sft import config

    cfg = await config.save(session, {"mode": "live", "ask_name": False}, "t")
    await say(session, cfg, "SFT", name="Meena K")
    assert rec.last()[1].startswith("Hi Meena!")


async def test_the_goal_leads_to_the_course_details_offer(session, cfg, rec):
    await say(session, cfg, "SFT")
    await say(session, cfg, "Priya")
    await say(session, cfg, button=c.B_SKILLS, title="Improve my skills")
    kind, body, _ = rec.last()
    assert kind == "buttons" and "South Indian food skills" in body
    assert rec.buttons() == ["View course details", "Ask a question"]
    lead = await _lead(session)
    assert lead.goal == "skills" and lead.stage == c.INTERESTED


async def test_view_course_details_sends_the_brochure_then_yes_no_ask(session, cfg, rec):
    lead = await _to_brochure(session, cfg, rec)
    document = [s for s in rec.sent if s[0] == "document"]
    assert document and document[0][1] == c.BROCHURE_FILENAME
    assert document[0][2].endswith("/static/sft/shero-food-training-brochure.pdf")
    kind, body, _ = rec.last()
    assert kind == "buttons" and "Would you like to enroll now?" in body
    assert "Can't open the PDF?" in body                  # the link, in case the file fails
    assert rec.buttons() == ["Yes, enroll me", "Not right now", "Ask a question"]
    assert lead.stage == c.BROCHURE_SENT and lead.step == c.DECIDE


async def test_yes_sends_the_payment_link(session, cfg, rec):
    lead = await _to_brochure(session, cfg, rec)
    await say(session, cfg, button=c.B_ENROL, title="Yes, enroll me")
    kind, body, (url, label) = rec.last()
    assert kind == "link" and "*Course fee: $99*" in body
    assert url.endswith(f"/sft/enrol/{lead.pay_token}") and label == "Pay $99 to enroll"
    assert lead.stage == c.PAYMENT_PENDING and lead.link_sent_at is not None


async def test_typing_yes_after_the_brochure_works_too(session, cfg, rec):
    await _to_brochure(session, cfg, rec)
    await say(session, cfg, "yes")
    assert rec.last()[0] == "link"


async def test_not_right_now_goes_into_the_follow_ups(session, cfg, rec):
    lead = await _to_brochure(session, cfg, rec)
    await say(session, cfg, button=c.B_LATER, title="Not right now")
    assert "reply *SFT*" in rec.last()[1]
    assert lead.stage == c.NURTURE and lead.followups_sent == 0 and lead.nurture_until is None


async def test_ask_a_question_after_the_brochure_goes_to_a_person(session, cfg, rec):
    lead = await _to_brochure(session, cfg, rec)
    await say(session, cfg, button=c.B_ASK, title="Ask a question")
    assert "type your question" in rec.last()[1] and lead.step == c.QUESTION

    rec.clear()
    await say(session, cfg, "Can I take the course from Canada?")
    texts = [body for kind, body, _ in rec.sent if kind == "text"]
    assert texts == [c.HANDED_OVER.format(first="Priya")]
    assert "handover" in rec.kinds()
    assert lead.with_team and lead.question == "Can I take the course from Canada?"
    assert lead.needs_attention and lead.attention_priority == "high"
    assert lead.stage == c.QUESTIONS


async def test_with_the_team_the_bot_says_nothing_at_all(session, cfg, rec):
    await _to_brochure(session, cfg, rec)
    await say(session, cfg, button=c.B_ASK, title="Ask a question")
    await say(session, cfg, "Is there a discount?")
    rec.clear()
    for message in ("hello?", "SFT", "Enroll now"):
        assert await say(session, cfg, message) == "with team"
    assert rec.sent == []
    # Still recorded for the team.
    said = (await session.execute(select(SftEvent.detail).where(SftEvent.kind == "inbound"))
            ).scalars().all()
    assert "hello?" in said


async def test_a_typed_question_anywhere_goes_to_the_team(session, cfg, rec):
    await say(session, cfg, "SFT")
    await say(session, cfg, "Priya")
    await say(session, cfg, "Do I need to know how to cook already?")
    lead = await _lead(session)
    assert lead.with_team and rec.sent[-2][1] == c.HANDED_OVER.format(first="Priya")


async def test_small_talk_gets_the_options_again_not_the_team(session, cfg, rec):
    await say(session, cfg, "SFT")
    await say(session, cfg, "Priya")
    await say(session, cfg, "ok")
    assert rec.last()[1] == c.TAP_BELOW and not (await _lead(session)).with_team


async def test_ask_before_the_brochure_opens_the_questions(session, cfg, rec):
    await say(session, cfg, "SFT")
    await say(session, cfg, "Priya")
    await say(session, cfg, button=c.B_BUSINESS, title="Start food business")
    await say(session, cfg, button=c.B_ASK, title="Ask a question")
    kind, _, rows = rec.last()
    assert kind == "list" and [t for _, t in rows][-1] == "Ask my own question"
    assert (await _lead(session)).stage == c.QUESTIONS

    await say(session, cfg, button="sft:faq:cert", title="Do I get a certificate?")
    assert "isn't automatic on payment" in rec.last()[1]
    assert rec.buttons() == ["View course details", "Enroll now", "Another question"]

    await say(session, cfg, button="sft:faq:own", title="Ask my own question")
    await say(session, cfg, "When does the next batch start?")
    assert (await _lead(session)).with_team


async def test_every_faq_answer_is_from_the_brochure(session, cfg, rec):
    answers = " ".join(c.FAQ_ANSWERS.values())
    for fact in ("10 short video lessons", "52 minutes", "10 quizzes", "laptop or desktop",
                 "approval is granted", "doesn't guarantee"):
        assert fact in answers


async def test_maybe_later_asks_when_and_schedules_it(session, cfg, rec):
    lead = await _to_brochure(session, cfg, rec)
    await say(session, cfg, "Maybe later")                 # a follow-up's quick reply
    assert rec.buttons() == ["In a week", "In a month", "I'll message you"]
    await say(session, cfg, button=c.B_WEEK, title="In a week")
    assert "in a week" in rec.last()[1]
    assert lead.stage == c.NURTURE and lead.nurture_until is not None


async def test_not_interested_stops_and_asks_why(session, cfg, rec):
    lead = await _to_brochure(session, cfg, rec)
    await say(session, cfg, "Not interested")
    assert rec.buttons() == ["Price", "Timing", "Something else"]
    assert lead.stage == c.LOST and lead.opted_out
    await say(session, cfg, button="sft:why:price", title="Price")
    assert lead.lost_reason == "Price"


async def test_coming_back_after_saying_no_starts_again(session, cfg, rec):
    lead = await _to_brochure(session, cfg, rec)
    await say(session, cfg, "Not interested")
    rec.clear()
    await say(session, cfg, "SFT")
    assert not lead.opted_out
    assert rec.sent[0][1].startswith("Welcome back, Priya")


async def test_stop_opts_out(session, cfg, rec):
    await say(session, cfg, "SFT")
    await say(session, cfg, "STOP")
    lead = await _lead(session)
    assert lead.opted_out and lead.stage == c.LOST and "won't get any more" in rec.last()[1]


async def test_each_stage_is_stamped_once_for_the_funnel(session, cfg, rec):
    lead = await _to_brochure(session, cfg, rec)
    await say(session, cfg, "yes")
    assert list(lead.stage_times) == [c.NEW, c.CONTACTED, c.INTERESTED, c.BROCHURE_SENT,
                                      c.PAYMENT_PENDING]
    stages = (await session.execute(select(SftEvent).where(SftEvent.kind == "stage"))
              ).scalars().all()
    assert all(e.crm_pending for e in stages)               # each goes to Zoho as a note


async def test_a_source_code_files_the_lead_under_it(session, cfg, rec):
    await say(session, cfg, "SFT IG")
    assert (await _lead(session)).source == "Instagram"


async def test_a_click_to_whatsapp_ad_is_a_meta_ad_lead(session, cfg, rec):
    await say(session, cfg, "Hi, tell me about SFT", ad_id="120208",
              referral={"source_id": "120208", "headline": "Shero Food Training"})
    lead = await _lead(session)
    assert lead.source == "Meta Ad" and lead.ad_id == "120208"
    assert lead.source_detail == "Shero Food Training"


async def test_a_failed_send_never_stops_the_journey(session, cfg, rec, monkeypatch):
    from app.core.exceptions import IntegrationError

    async def refuse(*a, **k):
        raise IntegrationError("gallabox", "document refused")

    monkeypatch.setattr(rec, "send_document", refuse)
    lead = await _to_brochure(session, cfg, rec)
    assert lead.step == c.DECIDE and rec.last()[0] == "buttons"
    failed = (await session.execute(select(SftEvent).where(SftEvent.kind == "send_failed"))
              ).scalars().all()
    assert len(failed) == 1


async def test_the_british_spelling_works_too(session, cfg, rec):
    await _to_brochure(session, cfg, rec)
    await say(session, cfg, "Enrol now")
    assert rec.last()[0] == "link" and "Pay $99 to enroll" == rec.last()[2][1]
