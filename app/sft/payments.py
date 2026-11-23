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

from datetime import timedelta
from decimal import Decimal

import stripe
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import PaymentError
from app.core.logging import get_logger
from app.db.models import SftLead
from app.integrations.stripe_gw import checkout
from app.sft import content as c
from app.sft import leads, sending
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
    """No real charge: no Stripe key, or one of SFT's test numbers in Test mode."""
    return checkout.mock_mode() or (cfg.mode == TEST and cfg.answers(lead.whatsapp_number))


async def checkout_url(session: AsyncSession, lead: SftLead, cfg: SftConfig) -> str:
    """A live Stripe Checkout for this lead: the open one, or a new one."""
    expires = aware(lead.checkout_expires_at)
    if lead.checkout_url and expires and expires - now() > timedelta(minutes=10):
        return lead.checkout_url
    checkout._apply_api_key()
    base = settings.public_base_url.rstrip("/")
    expires_at = now() + timedelta(hours=SESSION_HOURS)
    params = {
        "mode": "payment",
        "line_items": [{
            "price_data": {
                "currency": cfg.currency,
                "unit_amount": checkout.to_minor_units(cfg.course_fee),
                "product_data": {"name": PRODUCT, "description": PRODUCT_TEXT},
            },
            "quantity": 1,
        }],
        "client_reference_id": reference(lead),
        "metadata": {"kind": KIND, "sft_lead_id": str(lead.id), "sft_token": lead.pay_token,
                     "whatsapp_number": lead.whatsapp_number},
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
    await event(session, lead, "payment", "Checkout opened", data={"session": created.id})
    return created.url


async def clicked(session: AsyncSession, lead: SftLead) -> None:
    if lead.link_clicked_at is None:
        lead.link_clicked_at = now()
        await event(session, lead, "payment", "Opened the payment link", crm=True)
    leads.touch(lead)


async def mark_paid(session: AsyncSession, lead: SftLead, cfg: SftConfig, *,
                    amount: Decimal | None, currency: str | None,
                    payment_intent: str | None = None, email: str | None = None,
                    test: bool = False) -> bool:
    """The lead has paid: enrolled, told, and given the course. Once only."""
    if lead.paid_at is not None:
        return False
    lead.paid_at = now()
    lead.amount_paid = amount if amount is not None else cfg.course_fee
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
    if cfg.course_url:
        note = f"\n\n{cfg.course_access_note}" if cfg.course_access_note else ""
        await sending.link(session, lead, cfg,
                           c.PAID.format(first=first, amount=amount_text, note=note),
                           url=cfg.course_url, label=c.COURSE_BUTTON, what="paid")
        lead.course_link_sent_at = now()
    else:
        await sending.text(session, lead, cfg, c.PAID_NO_LINK.format(first=first,
                                                                     amount=amount_text),
                           what="paid")
        await leads.raise_attention(session, lead, "course_link")
    if not sending.in_window(lead):
        await leads.raise_attention(
            session, lead, "course_link",
            detail="Paid outside WhatsApp's 24-hour window, so the confirmation may not have "
                   "reached them: send the course access")
    log.info("sft_paid", lead_id=str(lead.id), test=test)
    return True


async def failed(session: AsyncSession, lead: SftLead, cfg: SftConfig) -> None:
    if lead.paid_at:
        return
    await event(session, lead, "payment", "Payment failed", crm=True)
    if sending.in_window(lead):
        from app.sft.flow import enrol_url

        await sending.link(session, lead, cfg, c.PAYMENT_FAILED.format(first=leads.first(lead)),
                           url=enrol_url(lead), label="Try again", what="payment_failed")


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
