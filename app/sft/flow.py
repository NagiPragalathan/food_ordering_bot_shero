"""The SFT journey on WhatsApp: one inbound message in, the bot's answer out.

    1  Lead enters      name (asked when unknown), then the welcome with the
                        fee and two goals: Start food business / Improve my skills
    2  Choose interest  a line on how SFT helps that goal:
                        View course details / Ask a question
    3  Send brochure    the brochure PDF, then: Yes, enroll me / Not right now /
                        Ask a question
                          Yes            -> 5
                          Not right now  -> follow-ups (7), Nurture
                          Ask a question -> "type your question" -> the team (8)
    4  Questions        (Ask a question before the brochure) the FAQ list; each
                        answer offers the brochure, Enroll now or another question;
                        "Ask my own question" goes to the team
    5  Enrollment        the fee and a Pay button: a Stripe checkout
    6  Paid             (payments.py) confirmation and the course link, then a
                        day later "have you been able to open it?"
    7  Follow-ups       (nudges.py) after 24 hours, 48-72 hours, 5-7 days quiet
       Maybe later      when to check back: in a week / in a month / I'll message you
       Not interested   stop, and (optionally) why
    8  The team         a question hands the chat to a person: "we're
                        transferring you", then the bot says nothing more to
                        this lead - no replies, no follow-ups - until someone
                        hands it back on the SFT admin page

Every step is recorded on the lead (stage, timeline) and synced to Zoho.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import SftLead
from app.schemas.inbound import InboundEvent, InboundKind
from app.services.conversation.validators import clean_name
from app.services.reply_triggers import normalise
from app.sft import content as c
from app.sft import leads, matching, offers, sending
from app.sft.config import SftConfig
from app.sft.leads import event, move, now, touch

log = get_logger(__name__)

FUNNEL_ORDER = {stage: i for i, stage in enumerate(c.FUNNEL)}


# --- the entry point ---------------------------------------------------------------
async def handle(session: AsyncSession, ev: InboundEvent, cfg: SftConfig,
                 lead: SftLead | None = None) -> str:
    """Answer one message from a lead. Returns what happened, for the log."""
    lead = lead or await leads.get(session, ev.whatsapp_number)
    created = lead is None
    if lead is None:
        where = matching.origin(ev, cfg)
        lead = await leads.create(
            session, ev.whatsapp_number, profile_name=ev.contact_name, source=where.source,
            source_detail=where.detail, ad_id=ev.ad_id, campaign_id=ev.campaign_id,
            first_message=ev.text, channel_id=ev.channel_id or None)
    else:
        if ev.contact_name and not lead.profile_name:
            lead.profile_name = ev.contact_name.strip()[:120]
        if ev.channel_id:
            lead.channel_id = ev.channel_id
    lead.last_inbound_at = now()
    await event(session, lead, "inbound", _said(ev),
                data={"reply_id": ev.reply_id or None, "kind": str(ev.kind)})

    if lead.with_team:
        # A person has this chat. The bot says nothing until they hand it back.
        log.info("sft_with_team_silent", lead_id=str(lead.id))
        return "with team"

    choice = matching.intent(ev)
    text = (ev.text or "").strip()

    if matching.stop(text):
        return await _stop(session, lead, cfg)
    if not created and lead.stage == c.NEW:
        await move(session, lead, c.CONTACTED, "replied")
    applied = choice is None and lead.step != c.QUESTION and \
        await _apply_code(session, lead, cfg, text)
    restart = choice is None and lead.step != c.QUESTION and (
        matching.from_menu(ev)
        or (matching.has_keyword(text, cfg) and not matching.is_question(text)))
    if created or restart or (applied and (lead.opted_out or lead.stage == c.LOST)):
        if lead.opted_out or lead.stage == c.LOST:
            lead.opted_out = False
            await event(session, lead, "back", "Came back after saying no", crm=True)
        return await start(session, lead, cfg, again=not created)
    if applied:
        # The code was the message: the question they are at, at the new price.
        return await _prompt_again(session, lead, cfg)
    if choice:
        return await _choose(session, lead, cfg, choice, ev)
    return await _typed(session, lead, cfg, ev)


async def _apply_code(session, lead, cfg, text: str) -> bool:
    """An offer code in the message: the lead's price from now on (Offers page)."""
    offer = await offers.from_message(session, lead, text, cfg)
    if offer is None or offer.id == lead.offer_id:
        return False
    before = await offers.price(session, lead, cfg)
    lead.offer_id = offer.id
    lead.offer_code = offer.code
    after = await offers.price(session, lead, cfg)
    if after.offer is None or after.offer.id != offer.id:
        # A better offer already applies to them: keep it, and say so.
        kept = before.offer if before.offer is not None and before.offer.code else None
        lead.offer_id = kept.id if kept else None
        lead.offer_code = kept.code if kept else None
        await sending.text(session, lead, cfg,
                           f"You already have our best price: {offers.fee_text(cfg, before)}. 🙂",
                           what="offer_not_better")
        return True
    await event(session, lead, "offer", f"Applied code {offer.code}: {cfg.money(after.amount)}",
                crm=True, data={"offer": str(offer.id), "code": offer.code,
                                "price": str(after.amount)})
    await sending.text(session, lead, cfg,
                       c.say(cfg, "offer_applied", first=leads.first(lead), code=offer.code,
                             fee=cfg.money(after.amount), was=cfg.money(after.fee)),
                       what="offer_applied")
    return True


async def fee(session, lead, cfg) -> str:
    """The fee as this lead's messages say it ("$79 (was $99)" with an offer)."""
    return offers.fee_text(cfg, await offers.price(session, lead, cfg))


async def start(session: AsyncSession, lead: SftLead, cfg: SftConfig, *,
                again: bool = False) -> str:
    """The welcome, or where the lead left off when they come back."""
    touch(lead)
    if lead.stage == c.ENROLLED:
        return await _already_paid(session, lead, cfg)
    if not lead.name:
        if cfg.ask_name:
            lead.step = c.ASK_NAME
            await sending.text(session, lead, cfg, c.say(cfg, "ask_name"), what="ask_name")
            return "asked name"
        lead.name = clean_name(lead.profile_name)
    if again and lead.goal:
        return await _resume(session, lead, cfg)
    return await _welcome(session, lead, cfg)


# --- steps -------------------------------------------------------------------------
async def _welcome(session, lead, cfg) -> str:
    lead.step = c.GOAL
    await sending.buttons(session, lead, cfg,
                          c.say(cfg, "welcome", first=leads.first(lead),
                                fee=await fee(session, lead, cfg)),
                          c.GOAL_BUTTONS, what="welcome")
    return "welcome"


async def _resume(session, lead, cfg) -> str:
    """Back after a while: their last open question again."""
    await sending.text(session, lead, cfg, c.say(cfg, "welcome_back", first=leads.first(lead)),
                       what="welcome_back")
    if lead.stage == c.PAYMENT_PENDING:
        return await _enrol(session, lead, cfg)
    if leads.reached(lead, c.BROCHURE_SENT):
        lead.step = c.DECIDE
        await sending.buttons(session, lead, cfg, await _brochure_text(session, lead, cfg),
                              c.DECIDE_BUTTONS, what="decide")
        return "resumed at the brochure"
    return await _interest(session, lead, cfg)


async def _goal(session, lead, cfg, goal: str) -> str:
    lead.goal = goal
    touch(lead)
    await event(session, lead, "goal", c.GOALS[goal], crm=True, data={"goal": goal})
    await _advance(session, lead, c.INTERESTED, c.GOALS[goal])
    return await _interest(session, lead, cfg)


async def _interest(session, lead, cfg) -> str:
    lead.step = c.DETAILS
    key = "interest_business" if lead.goal == "business" else "interest_skills"
    await sending.buttons(session, lead, cfg, c.say(cfg, key, first=leads.first(lead)),
                          c.DETAIL_BUTTONS, what="interest")
    return "interest"


async def _brochure(session, lead, cfg) -> str:
    touch(lead)
    url = cfg.brochure_link()
    await sending.document(session, lead, cfg, url=url, filename=c.BROCHURE_FILENAME,
                           caption=c.BROCHURE_CAPTION)
    lead.step = c.DECIDE
    await sending.buttons(session, lead, cfg, await _brochure_text(session, lead, cfg),
                          c.DECIDE_BUTTONS, what="decide")
    await _advance(session, lead, c.BROCHURE_SENT, "viewed the course details")
    return "brochure"


async def _brochure_text(session, lead, cfg: SftConfig) -> str:
    return (c.say(cfg, "brochure", fee=await fee(session, lead, cfg))
            + c.BROCHURE_LINK_LINE.format(url=cfg.brochure_link()))


async def _faq(session, lead, cfg) -> str:
    touch(lead)
    lead.step = c.FAQ
    await sending.faq_list(session, lead, cfg,
                           c.say(cfg, "faq_prompt", first=leads.first(lead)))
    await _advance(session, lead, c.QUESTIONS, "opened the questions")
    return "faq"


async def _answer(session, lead, cfg, faq_id: str) -> str:
    touch(lead)
    lead.step = c.FAQ
    await event(session, lead, "faq", faq_id.rsplit(":", 1)[-1], data={"faq": faq_id})
    await sending.buttons(session, lead, cfg,
                          c.say(cfg, c.FAQ_TEXTS[faq_id], fee=await fee(session, lead, cfg)),
                          c.AFTER_FAQ_BUTTONS, what="faq_answer")
    await _advance(session, lead, c.QUESTIONS, "read an answer")
    return "faq answer"


async def _ask(session, lead, cfg) -> str:
    """Ask a question: before the brochure the FAQ list; once they have seen
    it, their own question for the team."""
    if not leads.reached(lead, c.BROCHURE_SENT):
        return await _faq(session, lead, cfg)
    return await _type_question(session, lead, cfg)


async def _type_question(session, lead, cfg) -> str:
    touch(lead)
    lead.step = c.QUESTION
    await sending.text(session, lead, cfg, c.say(cfg, "ask_question", first=leads.first(lead)),
                       what="ask_question")
    await _advance(session, lead, c.QUESTIONS, "has a question")
    return "asked for the question"


async def to_team(session: AsyncSession, lead: SftLead, cfg: SftConfig, question: str, *,
                  key: str = "question", message: str = "handed_over") -> str:
    """Hand the chat to a person: tell the lead, then stay silent."""
    touch(lead)
    if question:
        lead.question = question[:4000]
    lead.with_team = True
    lead.handed_over_at = now()
    lead.step = c.WITH_TEAM
    if key == "question":
        await _advance(session, lead, c.QUESTIONS, "asked the team")
    await sending.text(session, lead, cfg, c.say(cfg, message, first=leads.first(lead)),
                       what="handed_over")
    await event(session, lead, "handover",
                f'With the team. Question: "{question[:400]}"' if question else "With the team",
                crm=True, data={"key": key})
    if lead.needs_attention:
        await leads.resolve(session, lead, "superseded: now with the team", by="the bot")
    # A second question later still reaches the team.
    lead.attention_raised = [k for k in (lead.attention_raised or []) if k != key]
    await leads.raise_attention(session, lead, key, detail=(f'"{question[:300]}"'
                                                            if question else ""))
    await sending.handover(lead, cfg, note=f"SFT: {c.DROPS[key].trigger}")
    await _alert_team(session, lead, cfg, c.DROPS[key].trigger)
    return "with the team"


async def _alert_team(session, lead, cfg, reason: str) -> None:
    """Tell the team's numbers (SFT settings) that a lead is waiting."""
    from app.integrations.gallabox.client import use_channel
    from app.integrations.gallabox.sender import current_sender

    for number in cfg.team_numbers:
        try:
            with use_channel(lead.channel_id or cfg.channel_id or None):
                await current_sender().send_template(
                    number, c.TEAM_ALERT, lead.name or lead.profile_name or "Unknown",
                    f"+{lead.whatsapp_number}", reason)
        except Exception as exc:  # noqa: BLE001 - an alert never breaks the chat
            log.warning("sft_team_alert_failed", error=str(exc), number_tail=number[-4:])


async def _enrol(session, lead, cfg) -> str:
    if lead.paid_at:
        return await _already_paid(session, lead, cfg)
    touch(lead)
    lead.step = c.PAY
    lead.link_sent_at = now()
    lead.payment_reminded_at = None
    price = await offers.price(session, lead, cfg)
    await sending.link(session, lead, cfg,
                       c.say(cfg, "enrol", first=leads.first(lead),
                             fee=offers.fee_text(cfg, price)),
                       url=enrol_url(lead), label=c.PAY_BUTTON.format(fee=cfg.money(price.amount)),
                       what="enroll")
    await _advance(session, lead, c.PAYMENT_PENDING, "wants to enroll")
    return "payment link"


def enrol_url(lead: SftLead) -> str:
    """The lead's one enroll link: it opens a live Stripe checkout every time."""
    return f"{settings.public_base_url.rstrip('/')}/sft/enrol/{lead.pay_token}"


async def _already_paid(session, lead, cfg) -> str:
    from app.sft import payments, platform

    lead.step = c.IDLE
    body = c.say(cfg, "already_paid", first=leads.first(lead))
    making = platform.wanted(lead, cfg)
    if making:
        # Their course account is still to be made (a miss at payment time).
        await platform.enrol(session, lead, cfg)
    if lead.platform_enrolled_at:
        # Their Shero Training login (the course platform made the account).
        await platform.send_login(session, lead, cfg)
    elif making:
        await sending.text(session, lead, cfg, body.split("!")[0] + "! 🎉 We're setting up your "
                           "course account now. Your login comes here in a few minutes.",
                           what="already_paid")
    elif cfg.course_url:
        await sending.link(session, lead, cfg, body, url=cfg.course_url, label=c.COURSE_BUTTON,
                           what="course_link")
    else:
        await sending.text(session, lead, cfg, body.split("!")[0] + "! 🎉 Our team will be in "
                           "touch with your course access.", what="already_paid")
    await payments.send_receipt(session, lead, cfg)
    return "already enrolled"


async def _not_now(session, lead, cfg) -> str:
    """Not right now (after the brochure): the automated follow-ups take it."""
    touch(lead)
    lead.step = c.IDLE
    await sending.text(session, lead, cfg, c.say(cfg, "not_now", first=leads.first(lead)),
                       what="not_now")
    await move(session, lead, c.NURTURE, "not right now")
    return "not now"


async def _maybe_later(session, lead, cfg) -> str:
    touch(lead)
    lead.step = c.LATER
    await sending.buttons(session, lead, cfg, c.say(cfg, "later", first=leads.first(lead)),
                          c.WHEN_BUTTONS, what="maybe_later")
    await move(session, lead, c.NURTURE, "maybe later")
    return "maybe later"


async def _when(session, lead, cfg, choice: str) -> str:
    touch(lead)
    lead.step = c.IDLE
    if choice == c.B_THEM:
        lead.nurture_until = None
        lead.nurture_sent = len(cfg.nurture_days)        # no automated check-ins
        lead.followups_sent = max(lead.followups_sent, len(cfg.followup_hours))
        await sending.text(session, lead, cfg, c.say(cfg, "later_them"), what="later_them")
        await event(session, lead, "nurture", "Will message us themselves", crm=True)
        return "they will message"
    days = 7 if choice == c.B_WEEK else 30
    lead.nurture_until = now() + timedelta(days=days)
    lead.nurture_sent = 0
    lead.followups_sent = max(lead.followups_sent, len(cfg.followup_hours))
    when = "in a week" if days == 7 else "in a month"
    await sending.text(session, lead, cfg, c.say(cfg, "later_set", when=when),
                       what="later_set")
    await event(session, lead, "nurture", f"Check back {when}", crm=True, data={"days": days})
    return f"check back {when}"


async def _not_interested(session, lead, cfg) -> str:
    touch(lead)
    lead.opted_out = True
    lead.step = c.LOST_REASON
    await sending.buttons(session, lead, cfg,
                          c.say(cfg, "not_interested", first=leads.first(lead)),
                          c.WHY_BUTTONS, what="not_interested")
    await move(session, lead, c.LOST, "not interested")
    if lead.needs_attention:
        await leads.resolve(session, lead, "said not interested", by="the bot")
    return "not interested"


async def _why(session, lead, cfg, choice: str) -> str:
    lead.lost_reason = c.WHY_IDS[choice]
    lead.step = c.IDLE
    await event(session, lead, "lost_reason", f"Reason: {lead.lost_reason}", crm=True)
    await sending.text(session, lead, cfg, c.say(cfg, "lost_thanks"), what="lost_thanks")
    return "lost reason"


async def _stop(session, lead, cfg) -> str:
    lead.opted_out = True
    lead.step = c.IDLE
    await sending.text(session, lead, cfg, c.say(cfg, "stopped"), what="stopped")
    await move(session, lead, c.LOST, "asked to stop")
    if lead.needs_attention:
        await leads.resolve(session, lead, "asked to stop", by="the bot")
    return "stopped"


async def _access(session, lead, cfg, ok: bool) -> str:
    touch(lead)
    if ok:
        lead.access_confirmed_at = lead.access_confirmed_at or now()
        lead.step = c.IDLE
        await event(session, lead, "access", "Opened the course", crm=True)
        await sending.text(session, lead, cfg, c.say(cfg, "access_ok", first=leads.first(lead)),
                           what="access_ok")
        if lead.needs_attention and lead.attention_stage in ("access", "course_link"):
            await leads.resolve(session, lead, "opened the course", by="the bot")
        return "course opened"
    return await to_team(session, lead, cfg, "Needs help opening the course",
                         key="access_help", message="access_help")


# --- what the lead said ---------------------------------------------------------------
async def _choose(session, lead, cfg, choice: str, ev: InboundEvent) -> str:
    if choice == c.B_BUSINESS:
        return await _goal(session, lead, cfg, "business")
    if choice == c.B_SKILLS:
        return await _goal(session, lead, cfg, "skills")
    if choice == c.B_DETAILS:
        return await _brochure(session, lead, cfg)
    if choice == c.B_ASK:
        return await _ask(session, lead, cfg)
    if choice == c.B_FAQ:
        return await _faq(session, lead, cfg)
    if choice == "sft:faq:own":
        return await _type_question(session, lead, cfg)
    if choice in c.FAQ_ANSWERS:
        return await _answer(session, lead, cfg, choice)
    if choice == c.B_ENROL:
        return await _enrol(session, lead, cfg)
    if choice == c.B_LATER:
        return await _not_now(session, lead, cfg)
    if choice == c.B_MAYBE:
        return await _maybe_later(session, lead, cfg)
    if choice in (c.B_WEEK, c.B_MONTH, c.B_THEM):
        return await _when(session, lead, cfg, choice)
    if choice == c.B_NO:
        return await _not_interested(session, lead, cfg)
    if choice in c.WHY_IDS:
        return await _why(session, lead, cfg, choice)
    if choice in (c.B_ACCESS_OK, c.B_ACCESS_HELP):
        return await _access(session, lead, cfg, choice == c.B_ACCESS_OK)
    log.info("sft_unknown_button", choice=choice)
    return await _prompt_again(session, lead, cfg)


async def _typed(session, lead, cfg, ev: InboundEvent) -> str:
    """Free text (or a photo, a location...) where the bot wanted something."""
    text = (ev.text or "").strip()
    step = lead.step
    if step == c.ASK_NAME:
        name = clean_name(text) if ev.kind is InboundKind.TEXT else None
        if name and (matching.has_keyword(name, cfg) or len(name.split()) > 5
                     or matching.is_question(name) or normalise(name) in matching.SMALL_TALK):
            name = None
        if not name:
            await sending.text(session, lead, cfg, c.say(cfg, "ask_name_again"),
                               what="ask_name_again")
            return "asked name again"
        lead.name = name.title() if name.islower() or name.isupper() else name
        lead.crm_dirty = True
        await event(session, lead, "name", f"Name: {lead.name}", crm=True)
        return await _welcome(session, lead, cfg)
    if step == c.QUESTION:
        question = text or f"[{ev.kind}]"
        return await to_team(session, lead, cfg, question)
    if step == c.GOAL and "?" not in text:
        said = normalise(text)
        if "business" in said.split() or "sell" in said.split():
            return await _goal(session, lead, cfg, "business")
        if any(w in said.split() for w in ("skill", "skills", "learn", "cook", "cooking")):
            return await _goal(session, lead, cfg, "skills")
    if step in (c.DECIDE, c.PAY, c.FAQ) and matching.yes(text):
        return await _enrol(session, lead, cfg)
    if step == c.DECIDE and matching.no(text):
        return await _not_now(session, lead, cfg)
    if step == c.ACCESS and (matching.yes(text) or matching.no(text)):
        return await _access(session, lead, cfg, matching.yes(text))
    if step == c.LOST_REASON and text:
        lead.lost_reason = text[:60]
        lead.step = c.IDLE
        await event(session, lead, "lost_reason", f"Reason: {text[:200]}", crm=True)
        await sending.text(session, lead, cfg, c.say(cfg, "lost_thanks"), what="lost_thanks")
        return "lost reason"
    if matching.is_question(text):
        return await to_team(session, lead, cfg, text)
    return await _prompt_again(session, lead, cfg)


async def _prompt_again(session, lead, cfg) -> str:
    """Nothing to act on: the question they are at, once more."""
    step = lead.step
    if step == c.GOAL:
        await sending.buttons(session, lead, cfg, c.say(cfg, "tap_below"), c.GOAL_BUTTONS, what="tap_below")
    elif step == c.DETAILS:
        await sending.buttons(session, lead, cfg, c.say(cfg, "tap_below"), c.DETAIL_BUTTONS, what="tap_below")
    elif step == c.DECIDE:
        await sending.buttons(session, lead, cfg, c.say(cfg, "tap_below"), c.DECIDE_BUTTONS, what="tap_below")
    elif step == c.FAQ:
        await sending.faq_list(session, lead, cfg,
                               c.say(cfg, "faq_prompt", first=leads.first(lead)))
    elif step == c.LATER:
        await sending.buttons(session, lead, cfg, c.say(cfg, "tap_below"), c.WHEN_BUTTONS, what="tap_below")
    elif step == c.ACCESS:
        await sending.buttons(session, lead, cfg, c.say(cfg, "tap_below"), c.ACCESS_BUTTONS, what="tap_below")
    elif step == c.PAY:
        return await _enrol(session, lead, cfg)
    elif step == c.LOST_REASON:
        await sending.buttons(session, lead, cfg, c.say(cfg, "tap_below"), c.WHY_BUTTONS, what="tap_below")
    else:
        return await start(session, lead, cfg, again=True)
    return "prompted again"


async def _advance(session, lead, stage: str, why: str) -> None:
    """Forward along the funnel only; from Nurture or Lost, any step is forward."""
    current = lead.stage
    if current == c.ENROLLED:
        return
    if current in (c.NURTURE, c.LOST) or \
            FUNNEL_ORDER.get(stage, 0) > FUNNEL_ORDER.get(current, -1):
        await move(session, lead, stage, why)


def _said(ev: InboundEvent) -> str:
    if ev.reply_title or ev.reply_id:
        return f"Tapped: {ev.reply_title or ev.reply_id}"
    if ev.text:
        return ev.text[:500]
    return f"[{ev.kind}]"
