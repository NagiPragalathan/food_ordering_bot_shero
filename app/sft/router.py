"""Is this WhatsApp message SFT's? Asked first by the Gallabox webhook.

  * SFT off: never (nothing changes for anyone).
  * SFT's own number (its channel id is set): every message on it is SFT's.
    In Test mode, numbers not on the test list get nothing from the bot there.
  * Shero US's number (no channel of its own): a message is SFT's only when
    it asks for it - an SFT button, the keyword, an SFT ad - or comes from a
    lead the journey is waiting on (their name, their question, a follow-up's
    button). Everything else goes to the ordering bot exactly as before. In
    Test mode only the test numbers are taken.

Delivery receipts are left to the ordering bot's handling.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import is_unset, settings
from app.core.logging import get_logger
from app.db.models import SftLead
from app.integrations.gallabox import channel
from app.schemas.inbound import InboundEvent
from app.sft import config, content as c, flow, leads, matching, offers
from app.sft.config import SftConfig

log = get_logger(__name__)

# A lead part-way through the journey is still answered without the keyword
# for this long after the bot's last message (WhatsApp's own window).
TYPING_HOURS = 24
# Steps where the bot is waiting on the lead.
ACTIVE_STEPS = (c.ASK_NAME, c.QUESTION, c.GOAL, c.DETAILS, c.FAQ, c.DECIDE, c.PAY, c.LATER,
                c.ACCESS, c.LOST_REASON)
# A lead handed to the team stays the team's on a shared number this long:
# their messages are not given to the ordering bot meanwhile.
WITH_TEAM_DAYS = 7


def on_sft_channel(event: InboundEvent, cfg: SftConfig) -> bool:
    return bool(cfg.channel_id) and event.channel_id == cfg.channel_id


async def route(session: AsyncSession, event: InboundEvent) -> dict | None:
    """Handle the message when it is SFT's; None when it is not (the
    ordering bot carries on as before). `session` is Shero US's."""
    cfg = await config.load(session)
    if not cfg.is_on:
        return None
    own_number = on_sft_channel(event, cfg)
    if not own_number:
        if cfg.channel_id:
            return None            # SFT has its own number; this is another one
        if not _on_shero_us_number(event):
            return None
    if not event.is_actionable:
        return {"status": "ignored", "reason": "sft: unreadable"} if own_number else None
    if not cfg.answers(event.whatsapp_number):
        if own_number:
            log.info("sft_not_on_test_list", number_tail=event.whatsapp_number[-4:])
            return {"status": "ignored", "reason": "sft test mode"}
        return None

    lead = await leads.get(session, event.whatsapp_number)
    if not own_number and not _wants_sft(event, lead, cfg) and             not await _has_code(session, event, cfg):
        return None
    try:
        outcome = await flow.handle(session, event, cfg, lead)
    except Exception:
        log.exception("sft_flow_failed", number_tail=event.whatsapp_number[-4:])
        raise
    log.info("sft_message_handled", outcome=outcome, number_tail=event.whatsapp_number[-4:])
    return {"status": "ok", "reason": f"sft: {outcome}"}


def _on_shero_us_number(event: InboundEvent) -> bool:
    """Shero US's own channel (DST India's and other teams' are not SFT's)."""
    from app.core import tenants

    return tenants.current().is_default and channel.tenant_for(event) == tenants.current()


def _wants_sft(event: InboundEvent, lead: SftLead | None, cfg: SftConfig) -> bool:
    if (event.reply_id or "").startswith("sft:"):
        return True
    if matching.has_keyword(event.text, cfg) or matching.from_listed_ad(event, cfg):
        return True
    if matching.from_menu(event):
        return True                     # the welcome menu's Shero Food Training button
    if lead is None:
        return False
    if lead.with_team:
        since = leads.aware(lead.handed_over_at)
        return since is not None and leads.now() - since < timedelta(days=WITH_TEAM_DAYS)
    recent = leads.aware(lead.last_outbound_at)
    if recent is None:
        return False
    quiet = leads.now() - recent
    if matching.is_sft_label(event):
        # A follow-up template's quick reply ("Enrol now", "Maybe later"...).
        return quiet < timedelta(days=45)
    if lead.step in ACTIVE_STEPS and quiet < timedelta(hours=TYPING_HOURS):
        # Part-way through the journey: what they type is for SFT (a name, a
        # question), unless it is one of the ordering bot's own keywords.
        return not _for_the_ordering_bot(event)
    return False


async def _has_code(session: AsyncSession, event: InboundEvent, cfg: SftConfig) -> bool:
    """A running offer's code ("SFT50" from an ad) starts the journey too."""
    if not (event.text or "").strip():
        return False
    return offers.find_code(event.text, await offers.live_codes(session, cfg)) is not None


async def explain(session: AsyncSession, number: str, text: str,
                  cfg: SftConfig) -> tuple[bool, list[str]]:
    """Would SFT take this message from this number on Shero US's number, and
    why: the Bot replies page's "Check a message"."""
    from app.schemas.inbound import InboundKind

    why: list[str] = []
    digits = leads.digits(number)
    if not cfg.is_on:
        why.append("SFT is off, so the ordering bot answers every message. Below is what "
                   "would happen once SFT is on.")
    if cfg.channel_id:
        why.append("SFT has its own WhatsApp number: every message on it is SFT's, and "
                   "nothing on Shero US's number is.")
    if digits and cfg.mode == config.TEST and not cfg.answers(digits):
        why.append(f"Test mode: +{digits} is not on the test list, so the ordering bot "
                   "answers it.")
        return False, why
    event = InboundEvent(whatsapp_number=digits or "0", kind=InboundKind.TEXT, text=text,
                         message_id="check")
    lead = await leads.get(session, digits) if digits else None
    code = offers.find_code(text, await offers.live_codes(session, cfg))
    if matching.has_keyword(text, cfg):
        hit = next(k for k in cfg.keywords if matching.has_keyword(text, cfg.model_copy(
            update={"keywords": [k]})))
        why.append(f'It has the keyword "{hit}": SFT answers it.')
        return True, why
    if code:
        why.append(f"It carries the offer code {code}: SFT answers it, at the offer's price.")
        return True, why
    if matching.is_sft_label(event):
        why.append("It is one of SFT's button labels (a follow-up's quick reply).")
        taken = lead is not None and _wants_sft(event, lead, cfg)
        why.append("This number had SFT messages recently, so SFT answers it." if taken else
                   "But this number has no recent SFT messages, so the ordering bot answers.")
        return taken, why
    if lead is not None and _wants_sft(event, lead, cfg):
        if lead.with_team:
            why.append("This lead is with the team: SFT keeps their messages (the bot stays "
                       "silent) for the team to answer.")
        else:
            waiting = c.STEP_LABELS.get(lead.step) or lead.step
            why.append(f"This lead is part-way through the journey (the bot is waiting for "
                       f"{waiting}), so SFT answers.")
        return True, why
    if lead is not None and _for_the_ordering_bot(event):
        why.append("It is one of the ordering bot's own keywords, so the ordering bot answers.")
        return False, why
    why.append("No SFT keyword, code or button, and the number is not part-way through the "
               "journey: the ordering bot answers it.")
    return False, why


def _for_the_ordering_bot(event: InboundEvent) -> bool:
    from app.services import reply_triggers

    text = event.text or ""
    return bool(reply_triggers.matching(text)) or         reply_triggers.normalise(text) in reply_triggers.BUILT_IN


def ready() -> list[str]:
    """What is missing before SFT can take payments and give the course."""
    missing = []
    if is_unset(settings.stripe_secret_key):
        missing.append("Stripe key (payments run in test mode until it is set)")
    if "localhost" in settings.public_base_url:
        missing.append("PUBLIC_BASE_URL (the brochure and pay links need the public address)")
    return missing
