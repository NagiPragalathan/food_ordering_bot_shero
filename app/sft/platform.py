"""The Shero Training course platform (api.sherosft.com): a paid lead's account.

Once a lead has paid, the platform's payment webhook makes their account,
partner code (SFT-US-NNN) and course invite, and emails them the invite
(Shero_SFT_WhatsApp_Partner_Integration_API_Documentation, sections 4-6).
Students sign in on the platform's login page with that email and a 6-digit
code it emails them: there is no password. The webhook does not return the
invite link, and the link's token is not checked, so the bot sends the login
page itself (cfg.platform_login_url) with the email to use.

    ready()            the webhook secret is set (SFT Settings)
    wanted(lead, cfg)  a paid lead who should have an account and has none yet
    enrol(...)         one call to the webhook: DONE, RETRY, FAILED or SKIPPED
    send_login(...)    the login, on WhatsApp
    run(session)       the job (sft_platform): the retries
    check(cfg)         Settings' Check connection; it makes nothing

The platform trusts what it is told, so only payments this bot has confirmed
(Stripe, or a test payment when platform_for_tests is on) reach it. A
payment goes once (payment_id is the Stripe payment, or sft-[test-]<token>),
and repeating it is safe: the platform updates the same record.
"""

from __future__ import annotations

import re
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import is_unset, settings
from app.core.logging import get_logger
from app.db.models import SftLead
from app.sft import content as c
from app.sft import leads, sending
from app.sft.config import SftConfig
from app.sft.leads import event, now

log = get_logger(__name__)

DONE, RETRY, FAILED, SKIPPED = "done", "retry", "failed", "skipped"
BUSY = "busy"                      # the same payment is being sent right now
_posting: set[str] = set()         # payment ids on their way (one server process)
WEBHOOK = "/partner-payments/webhook"
TIMEOUT = 15.0
# Minutes between tries after a network error or a 5xx; then the team takes it.
RETRY_MINUTES = (1, 5, 15, 60, 240, 720)
TEST_CHAT = "Test chat"            # admin.TEST_SOURCE: never a real student
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")


def secret() -> str:
    value = settings.sft_platform_webhook_secret or ""
    return "" if is_unset(value) else value.strip()


def ready() -> bool:
    return bool(secret())


def valid_email(value: str | None) -> bool:
    return bool(EMAIL.fullmatch((value or "").strip()))


def payment_id(lead: SftLead) -> str:
    return lead.payment_intent_id or f"sft-{'test-' if lead.test_payment else ''}{lead.pay_token}"


def wanted(lead: SftLead, cfg: SftConfig) -> bool:
    """A paid lead the platform should get, and hasn't yet."""
    return (ready() and lead.paid_at is not None and lead.refunded_at is None
            and lead.platform_enrolled_at is None and lead.source != TEST_CHAT
            and (not lead.test_payment or cfg.platform_for_tests))


def body(lead: SftLead) -> dict:
    name = (lead.name or lead.profile_name or "").strip() or "Shero partner"
    return {"partner_name": name[:120], "partner_email": (lead.email or "").strip().lower(),
            "payment_id": payment_id(lead), "payment_status": "paid",
            "amount": float(lead.amount_paid or 0), "partner_id": f"WA-{lead.whatsapp_number}"}


async def _post(url: str, *, json: dict, headers: dict) -> tuple[int, dict]:
    """POST and the JSON answer (tests replace this: nothing leaves them)."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        answer = await client.post(url, json=json, headers=headers)
    try:
        data = answer.json()
    except ValueError:
        data = {}
    return answer.status_code, data if isinstance(data, dict) else {}


def _webhook(cfg: SftConfig) -> str:
    return cfg.platform_api_url.rstrip("/") + WEBHOOK


async def enrol(session: AsyncSession, lead: SftLead, cfg: SftConfig) -> str:
    """Make their account on the platform (it emails the invite)."""
    if not wanted(lead, cfg):
        return SKIPPED
    if not valid_email(lead.email):
        return await _failed(session, lead, "No email to make the course account with: add "
                                            "their email on the lead page, then Make the course "
                                            "account")
    pid = payment_id(lead)
    if pid in _posting:
        # Already on its way (the job and the lead page at once): never two
        # calls for one payment at the same moment (the platform's G8).
        return BUSY
    _posting.add(pid)
    try:
        status, data = await _post(_webhook(cfg), json=body(lead),
                                   headers={"x-webhook-secret": secret()})
    except httpx.HTTPError as exc:
        return await _retry(session, lead, f"Could not reach the course platform "
                                           f"({type(exc).__name__})")
    finally:
        _posting.discard(pid)
    error = str(data.get("error") or f"HTTP {status}")[:200]
    if status == 200 and data.get("ok"):
        lead.platform_id = str(data.get("id") or "")[:64] or None
        lead.platform_enrolled_at = now()
        lead.platform_error = None
        lead.platform_next_try_at = None
        await event(session, lead, "course_account",
                    f"Shero Training account made for {lead.email}; the platform emailed the "
                    "invite", crm=True,
                    data={"id": lead.platform_id, "payment_id": payment_id(lead)})
        if lead.needs_attention and lead.attention_stage == "course_account":
            await leads.resolve(session, lead, "course account made", by="the bot")
        log.info("sft_platform_enrolled", lead_id=str(lead.id), test=lead.test_payment)
        return DONE
    if status == 401:
        return await _failed(session, lead, f"The course platform refused the webhook secret "
                                            f"({error}): check it on SFT Settings")
    if 400 <= status < 500:
        return await _failed(session, lead, f"The course platform said: {error}")
    return await _retry(session, lead, f"The course platform said: {error}")


async def _retry(session: AsyncSession, lead: SftLead, why: str) -> str:
    lead.platform_attempts = (lead.platform_attempts or 0) + 1
    lead.platform_error = why[:300]
    log.warning("sft_platform_retry", lead_id=str(lead.id), attempt=lead.platform_attempts,
                error=why)
    if lead.platform_attempts > len(RETRY_MINUTES):
        return await _failed(session, lead, why)
    wait = RETRY_MINUTES[lead.platform_attempts - 1]
    lead.platform_next_try_at = now() + timedelta(minutes=wait)
    if lead.platform_attempts == 1:
        await event(session, lead, "course_account_retry",
                    f"Course account not made yet ({why}); trying again", crm=True)
    return RETRY


async def _failed(session: AsyncSession, lead: SftLead, why: str) -> str:
    lead.platform_error = why[:300]
    lead.platform_next_try_at = None
    await event(session, lead, "course_account_failed", why, crm=True)
    await leads.raise_attention(session, lead, "course_account", detail=why)
    log.error("sft_platform_failed", lead_id=str(lead.id), error=why)
    return FAILED


async def send_login(session: AsyncSession, lead: SftLead, cfg: SftConfig) -> bool:
    """How to sign in to the course, with the button to the login page."""
    text = c.say(cfg, "login", first=leads.first(lead), email=lead.email or "")
    went = await sending.link(session, lead, cfg, text, url=cfg.platform_login_url,
                              label=c.COURSE_LOGIN_BUTTON, what="login")
    if went:
        lead.login_sent_at = now()
        lead.course_link_sent_at = lead.course_link_sent_at or now()
    return went


async def run(session: AsyncSession) -> int:
    """The job: try the accounts that are due again; send the login to those
    made now while WhatsApp's 24-hour window is open (else on their next
    message, flow._already_paid)."""
    if not ready():
        return 0
    from app.sft import config

    cfg = await config.load(session)
    rows = (await session.execute(
        select(SftLead).where(SftLead.platform_next_try_at.is_not(None),
                              SftLead.platform_next_try_at <= now(),
                              SftLead.platform_enrolled_at.is_(None),
                              SftLead.paid_at.is_not(None))
        .order_by(SftLead.platform_next_try_at).limit(20))).scalars().all()
    made = 0
    for lead in rows:
        if await enrol(session, lead, cfg) == DONE:
            made += 1
            if sending.in_window(lead):
                await send_login(session, lead, cfg)
        elif not wanted(lead, cfg):
            lead.platform_next_try_at = None
    return made


async def check(cfg: SftConfig) -> tuple[bool, str]:
    """Is the platform there, and does it take the secret? Sends no student,
    so nothing is made: a good secret gets "details required" (400)."""
    if not ready():
        return False, "Paste the webhook secret from Shero first."
    try:
        status, data = await _post(_webhook(cfg), json={},
                                   headers={"x-webhook-secret": secret()})
    except httpx.HTTPError as exc:
        return False, f"Could not reach {cfg.platform_api_url} ({type(exc).__name__})."
    error = str(data.get("error") or "")
    if status == 401:
        return False, f"The course platform refused the secret ({error or 'HTTP 401'})."
    if status == 400:
        return True, ("Connected: the course platform took the secret and asked for a student's "
                      "details, so nothing was made.")
    return False, f"Unexpected answer from the course platform: HTTP {status} {error}".strip()
