"""Reading a WhatsApp message the SFT way: its keyword, its source, its button."""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas.inbound import InboundEvent
from app.services.reply_triggers import normalise
from app.sft import content as c
from app.sft.config import SftConfig

META_AD = "Meta Ad"
WHATSAPP = "WhatsApp"


def has_keyword(text: str | None, cfg: SftConfig) -> bool:
    """A keyword as whole words anywhere in the message ("SFT", "sft ig",
    "I'm interested in Shero Food Training")."""
    said = f" {normalise(text or '')} "
    return any(f" {normalise(k)} " in said for k in cfg.keywords if normalise(k))


def from_menu(event: InboundEvent) -> bool:
    """The welcome menu's Shero Food Training button (services/welcome_menu),
    tapped or its words typed: SFT's, whatever its label says."""
    from app.services import welcome_menu

    menu = welcome_menu.active()
    if menu is None:
        return False
    button = menu.button(event.reply_title or event.reply_id or event.text or "")
    return bool(button and button.to_sft)


def from_listed_ad(event: InboundEvent, cfg: SftConfig) -> bool:
    """A Click-to-WhatsApp ad that is SFT's: listed by id, or one whose
    headline or text carries a keyword."""
    if event.ad_id and event.ad_id in cfg.ad_ids:
        return True
    referral = event.referral or {}
    words = " ".join(str(referral.get(k) or "") for k in ("headline", "body", "source_url"))
    return bool(referral) and has_keyword(words, cfg)


def intent(event: InboundEvent) -> str | None:
    """The SFT button behind the message: its id when tapped, or a template
    quick reply's label (or the label typed out)."""
    reply_id = (event.reply_id or "").strip()
    if reply_id.startswith("sft:"):
        return reply_id
    for said in (event.reply_title, reply_id, event.text):
        key = us_spelling(normalise(said or ""))
        if key in c.LABELS:
            return c.LABELS[key]
    return None


def us_spelling(said: str) -> str:
    """"enrol now" (British) reads as "enroll now"."""
    words = said.split()
    return " ".join({"enrol": "enroll", "enrolment": "enrollment"}.get(w, w) for w in words)


def is_sft_label(event: InboundEvent) -> bool:
    return intent(event) is not None


@dataclass(frozen=True)
class Origin:
    source: str
    detail: str | None = None


def origin(event: InboundEvent, cfg: SftConfig) -> Origin:
    """Where a new lead came from: a Click-to-WhatsApp ad, a source code with
    the keyword ("SFT IG", a wa.me link's text), or straight to WhatsApp."""
    if event.ad_id or event.referral:
        referral = event.referral or {}
        headline = str(referral.get("headline") or "").strip()
        return Origin(META_AD, headline or (f"ad {event.ad_id}" if event.ad_id else None))
    if from_menu(event):
        return Origin(WHATSAPP, "welcome menu")
    said = normalise(event.text or "")
    words = said.split()
    for code, name in cfg.source_codes.items():
        if normalise(code) in words or (normalise(name) and f" {normalise(name)} " in f" {said} "):
            return Origin(name, f"code {code}")
    return Origin(WHATSAPP)


QUESTION_WORDS = ("what", "how", "when", "where", "why", "who", "which", "can", "could", "do",
                  "does", "is", "are", "will", "would", "should")
SMALL_TALK = {"hi", "hello", "hey", "hii", "ok", "okay", "k", "thanks", "thank you", "thx",
              "yes", "no", "sure", "fine", "great", "good", "nice", "cool", "done"}


def is_question(text: str | None) -> bool:
    """Free text that reads like a question for the team, not "ok" or "hi"."""
    raw = (text or "").strip()
    said = normalise(raw)
    if not said or said in SMALL_TALK:
        return False
    words = said.split()
    return "?" in raw or len(words) >= 4 or (len(words) >= 2 and words[0] in QUESTION_WORDS)


def yes(text: str | None) -> bool:
    return normalise(text or "") in {"yes", "y", "yeah", "yep", "sure", "ok", "okay",
                                     "yes please", "enrol", "enroll", "enrol me", "enroll me"}


def no(text: str | None) -> bool:
    return normalise(text or "") in {"no", "n", "nope", "not now", "later", "no thanks",
                                     "not interested"}


def stop(text: str | None) -> bool:
    return normalise(text or "") in {"stop", "unsubscribe", "stop messages", "opt out"}
