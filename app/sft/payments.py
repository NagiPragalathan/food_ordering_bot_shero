"""Paying for the course: Stripe Checkout behind one stable enroll link.

The lead's Pay button opens /sft/enrol/<token>. That page makes (or reuses) a
Stripe Checkout Session for the course fee and sends them to it, so the link
in their chat never expires, and a tap on it is counted (link clicked) for
the analytics.

Stripe tells us the outcome on the shared /webhooks/stripe endpoint; SFT's
sessions carry metadata kind=sft, which that endpoint hands here. The paid
page also asks Stripe directly, so a late webhook delays nothing. Both paths
are idempotent: a lead is marked paid once.

Without a Stripe key (a test setup), or for SFT's test numbers while SFT is
in Test mode, the link opens a test page that marks the lead paid without
charging anyone.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from decimal import Decimal

import stripe
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import PaymentError
from app.core.logging import get_logger
from app.db.models import SftLead, SftOffer
from app.integrations.stripe_gw import checkout
from app.sft import content as c
from app.sft import leads, offers, platform, receipt, sending
from app.sft.config import TEST, SftConfig
from app.sft.leads import aware, event, move, now

log = get_logger(__name__)

KIND = "sft"
PRODUCT = "Shero Food Training (SFT)"
PRODUCT_TEXT = ("5-day self-paced course: 10 short video lessons, 10 quizzes, 3 regional "
                "cuisines. Certification after the required approval.")
# Stripe allows a Checkout Session to live 24 hours at most.
SESSION_HOURS = 23


def reference(lead: SftLead) -> str:
    return f"SFT-{lead.pay_token}"


def test_checkout(lead: SftLead, cfg: SftConfig) -> bool:
    """No real charge: no Stripe key, or one of SFT's test numbers in Test mode
    while Test payments is on (Bot replies). Live mode always charges."""
    return checkout.mock_mode() or (cfg.mode == TEST and cfg.test_payments
                                    and cfg.answers(lead.whatsapp_number))


async def checkout_url(session: AsyncSession, lead: SftLead, cfg: SftConfig) -> str:
    """A live Stripe Checkout for this lead at their price: the open one, or a
    new one (also when an offer changed their price since it was opened)."""
    price = await offers.price(session, lead, cfg)
    expires = aware(lead.checkout_expires_at)
    same_price = lead.checkout_amount is None or Decimal(lead.checkout_amount) == price.amount
    if lead.checkout_url and expires and expires - now() > timedelta(minutes=10) and same_price:
        return lead.checkout_url
    checkout._apply_api_key()
    base = settings.public_base_url.rstrip("/")
    expires_at = now() + timedelta(hours=SESSION_HOURS)
    params = {
        "mode": "payment",
        "line_items": [{
            "price_data": {
                "currency": cfg.currency,
                "unit_amount": checkout.to_minor_units(price.amount),
                "product_data": {"name": PRODUCT, "description": PRODUCT_TEXT,
                                 "images": [f"{base}/static/brand/shero-logo-192.png"]},
            },
            "quantity": 1,
        }],
        "client_reference_id": reference(lead),
        "metadata": {"kind": KIND, "sft_lead_id": str(lead.id), "sft_token": lead.pay_token,
                     "whatsapp_number": lead.whatsapp_number,
                     "offer_code": (price.offer.code or price.offer.title)[:40]
                     if price.offer else ""},
        "payment_intent_data": {"metadata": {"kind": KIND, "sft_lead_id": str(lead.id),
                                             "sft_token": lead.pay_token},
                                "description": PRODUCT},
        "expires_at": int(expires_at.timestamp()),
        "success_url": f"{base}/sft/enrol/{lead.pay_token}/done",
        "cancel_url": f"{base}/sft/enrol/{lead.pay_token}",
    }
    if lead.email:
        params["customer_email"] = lead.email
    try:
        created = await checkout._create(params, reference(lead))
    except stripe.StripeError as exc:
        log.error("sft_checkout_failed", lead_id=str(lead.id), error=str(exc))
        raise PaymentError(f"Stripe checkout creation failed: {exc}") from exc
    lead.checkout_session_id = created.id
    lead.checkout_url = created.url
    lead.checkout_expires_at = expires_at
    lead.checkout_amount = price.amount
    if price.offer is not None and lead.offer_id != price.offer.id:
        lead.offer_id = price.offer.id               # an automatic offer: recorded on the lead
        lead.offer_code = price.offer.code
    await event(session, lead, "payment", f"Checkout opened: {cfg.money(price.amount)}",
                data={"session": created.id, "amount": str(price.amount)})
    return created.url


async def clicked(session: AsyncSession, lead: SftLead) -> None:
    if lead.link_clicked_at is None:
        lead.link_clicked_at = now()
        await event(session, lead, "payment", "Opened the payment link", crm=True)
    leads.touch(lead)


# Stripe's webhook and the lead's return to /done can confirm one payment at
# the same moment, each in its own request and session: the second must not
# enrol them again (a second confirmation, receipt and course-platform call;
# the platform asks for one call per payment, its G8). One server process
# (uvicorn, no workers), so a lock and a note per lead do it; the note lasts
# until the first request's commit has surely landed.
_paying: dict[str, asyncio.Lock] = {}
_paid_here: dict[str, float] = {}
PAID_MEMO_SECONDS = 600


async def mark_paid(session: AsyncSession, lead: SftLead, cfg: SftConfig, *,
                    amount: Decimal | None, currency: str | None,
                    payment_intent: str | None = None, email: str | None = None,
                    test: bool = False) -> bool:
    """The lead has paid: enrolled, told, and given the course. Once only."""
    key = str(lead.id)
    lock = _paying.setdefault(key, asyncio.Lock())
    async with lock:
        marked = _paid_here.get(key)
        if lead.paid_at is not None or (marked and time.monotonic() - marked < PAID_MEMO_SECONDS):
            return False
        _paid_here[key] = time.monotonic()
        try:
            return await _mark_paid(session, lead, cfg, amount=amount, currency=currency,
                                    payment_intent=payment_intent, email=email, test=test)
        except BaseException:
            _paid_here.pop(key, None)
            raise


async def _mark_paid(session: AsyncSession, lead: SftLead, cfg: SftConfig, *,
                     amount: Decimal | None, currency: str | None,
                     payment_intent: str | None, email: str | None, test: bool) -> bool:
    price = await offers.price(session, lead, cfg)
    if price.offer is not None and lead.offer_id != price.offer.id:
        lead.offer_id = price.offer.id
        lead.offer_code = price.offer.code
    lead.paid_at = now()
    lead.amount_paid = amount if amount is not None else price.amount
    lead.currency = (currency or cfg.currency).upper()[:3]
    lead.payment_intent_id = payment_intent
    lead.test_payment = test
    if email and not lead.email:
        lead.email = email[:255]
    leads.touch(lead)
    amount_text = _money(lead.amount_paid, lead.currency)
    await event(session, lead, "payment",
                f"Paid {amount_text}" + (" (TEST, nobody was charged)" if test else ""),
                crm=True, data={"amount": str(lead.amount_paid), "test": test,
                                "payment_intent": payment_intent})
    await move(session, lead, c.ENROLLED, "payment confirmed")
    if lead.needs_attention and lead.attention_stage == c.PAYMENT_PENDING:
        await leads.resolve(session, lead, "paid", by="the bot")
    lead.step = c.IDLE
    first = leads.first(lead)
    offer_title = price.offer.title if price.offer else None
    # Their account on the Shero Training course platform, which emails the
    # invite (platform.py); a miss is tried again by the sft_platform job.
    account = await platform.enrol(session, lead, cfg)
    on_platform = account != platform.SKIPPED
    if not sending.in_window(lead):
        # Only a template can reach them now: sft_enrolled, whose Get my course
        # opens the chat again for the course (or login) and the receipt PDF.
        _save_receipt(lead, cfg, offer_title)
        went = await sending.template(session, lead, cfg, c.ENROLLED_TEMPLATE, first,
                                      amount_text, receipt.number(lead))
        if not went or not (cfg.course_url or on_platform):
            await leads.raise_attention(
                session, lead, "course_link",
                detail="" if went else "Paid outside WhatsApp's 24-hour window and the "
                "confirmation could not go (sft_enrolled not approved): send the course access")
        log.info("sft_paid", lead_id=str(lead.id), test=test, outside_window=True)
        return True
    note = f"\n\n{cfg.course_access_note}" if cfg.course_access_note else ""
    if account == platform.DONE:
        if await sending.link(session, lead, cfg,
                              c.say(cfg, "paid_login", first=first, amount=amount_text,
                                    email=lead.email or "", note=note),
                              url=cfg.platform_login_url, label=c.COURSE_LOGIN_BUTTON,
                              what="paid"):
            lead.login_sent_at = lead.course_link_sent_at = now()
    elif on_platform:
        # Being made (tried again shortly) or for the team (a task is raised).
        await sending.text(session, lead, cfg,
                           c.say(cfg, "paid_setting_up", first=first, amount=amount_text),
                           what="paid")
    elif cfg.course_url:
        await sending.link(session, lead, cfg,
                           c.say(cfg, "paid", first=first, amount=amount_text, note=note),
                           url=cfg.course_url, label=c.COURSE_BUTTON, what="paid")
        lead.course_link_sent_at = now()
    else:
        await sending.text(session, lead, cfg,
                           c.say(cfg, "paid_no_link", first=first, amount=amount_text),
                           what="paid")
        await leads.raise_attention(session, lead, "course_link")
    await _send_receipt(session, lead, cfg, offer_title)
    log.info("sft_paid", lead_id=str(lead.id), test=test)
    return True


def _save_receipt(lead: SftLead, cfg: SftConfig, offer_title: str | None) -> str:
    """Draw and keep the receipt; '' when it worked, else why not."""
    try:
        receipt.save(lead, cfg, offer_title=offer_title)
    except Exception as exc:  # noqa: BLE001 - the payment stands whatever the PDF does
        log.error("sft_receipt_failed", lead_id=str(lead.id), error=str(exc))
        return str(exc) or "could not be drawn"
    return ""


async def send_receipt(session: AsyncSession, lead: SftLead, cfg: SftConfig) -> bool:
    """The receipt PDF again (a paid lead asking for their course)."""
    if lead.paid_at is None:
        return False
    if receipt.stored(lead.pay_token, receipt.filename(lead)) is None:
        offer = await session.get(SftOffer, lead.offer_id) if lead.offer_id else None
        return await _send_receipt(session, lead, cfg, offer.title if offer else None)
    return await sending.document(
        session, lead, cfg, url=receipt.url(lead), filename=receipt.filename(lead),
        caption=c.RECEIPT_CAPTION.format(number=receipt.number(lead)),
        what="receipt", label="Open the receipt")


async def _send_receipt(session: AsyncSession, lead: SftLead, cfg: SftConfig,
                        offer_title: str | None) -> bool:
    """The enrollment receipt, as a PDF in their WhatsApp chat. Best effort:
    a receipt that can't be drawn or sent never undoes the payment."""
    why = _save_receipt(lead, cfg, offer_title)
    if why:
        await event(session, lead, "send_failed", f"receipt: {why}"[:300],
                    data={"what": "receipt"})
        return False
    return await sending.document(
        session, lead, cfg, url=receipt.url(lead), filename=receipt.filename(lead),
        caption=c.RECEIPT_CAPTION.format(number=receipt.number(lead)),
        what="receipt", label="Open the receipt")


async def failed(session: AsyncSession, lead: SftLead, cfg: SftConfig) -> None:
    if lead.paid_at:
        return
    await event(session, lead, "payment", "Payment failed", crm=True)
    if sending.in_window(lead):
        from app.sft.flow import enrol_url

        await sending.link(session, lead, cfg,
                           c.say(cfg, "payment_failed", first=leads.first(lead)),
                           url=enrol_url(lead), label="Try again", what="payment_failed")
    else:
        # Complete enrollment (its quick reply) sends the checkout link again.
        await sending.template(session, lead, cfg, c.PAYMENT_FAILED_TEMPLATE, leads.first(lead))


async def handle_stripe(session: AsyncSession, event_type: str, obj: dict) -> dict:
    """One Stripe event for an SFT checkout (webhooks_stripe hands it here)."""
    from app.sft import config

    metadata = obj.get("metadata") or {}
    lead = await leads.by_token(session, str(metadata.get("sft_token") or ""))
    if lead is None:
        log.warning("sft_stripe_no_lead", type=event_type)
        return {"status": "ignored", "reason": "sft lead not found"}
    cfg = await config.load(session)
    match event_type:
        case "checkout.session.completed" | "checkout.session.async_payment_succeeded":
            if obj.get("payment_status") in ("paid", "no_payment_required"):
                details = obj.get("customer_details") or {}
                await mark_paid(session, lead, cfg,
                                amount=_from_minor(obj.get("amount_total")),
                                currency=obj.get("currency"),
                                payment_intent=_as_id(obj.get("payment_intent")),
                                email=details.get("email"))
        case "checkout.session.expired":
            await event(session, lead, "payment", "Checkout expired unpaid")
            if lead.checkout_session_id == obj.get("id"):
                lead.checkout_url = None
                lead.checkout_expires_at = None
        case "checkout.session.async_payment_failed" | "payment_intent.payment_failed":
            await failed(session, lead, cfg)
        case "charge.refunded":
            if lead.refunded_at is None:
                lead.refunded_at = now()
                await event(session, lead, "payment", "Refunded (from Stripe)", crm=True)
    return {"status": "ok", "type": event_type, "sft": True}


async def confirm_from_stripe(session: AsyncSession, lead: SftLead, cfg: SftConfig) -> bool:
    """Back on the paid page before the webhook: ask Stripe, mark paid if so."""
    if lead.paid_at or not lead.checkout_session_id or checkout.mock_mode():
        return bool(lead.paid_at)
    try:
        state = await checkout.session_state(lead.checkout_session_id)
    except PaymentError:
        return False
    if state and state.get("payment_status") == "paid":
        await mark_paid(session, lead, cfg, amount=None, currency=None,
                        payment_intent=state.get("payment_intent"))
        return True
    return False


def is_sft(obj: dict) -> bool:
    return (obj.get("metadata") or {}).get("kind") == KIND


def _from_minor(value) -> Decimal | None:
    try:
        return Decimal(int(value)) / 100 if value is not None else None
    except (TypeError, ValueError):
        return None


def _money(amount: Decimal | None, currency: str | None) -> str:
    text = f"{Decimal(amount or 0):,.2f}".removesuffix(".00")
    return f"${text}" if (currency or "").upper() == "USD" else f"{text} {currency or ''}".strip()


def _as_id(value) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("id")
    return None
