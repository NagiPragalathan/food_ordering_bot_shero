"""The pages behind SFT's Pay button: /sft/enrol/<token>.

    GET  /sft/enrol/<token>             review: the course, the price, Pay
    GET  /sft/enrol/<token>/pay         opens the live Stripe checkout
    POST /sft/enrol/<token>/test-paid   the test checkout's Pay: paid, nobody charged
    GET  /sft/enrol/<token>/done        back from Stripe: "you're enrolled"
    GET  /sft/receipt/<token>/<file>    the enrollment receipt PDF

The token is the lead's own random pay token, so a link identifies one lead
and nothing else (no number or name in the URL). Stripe's Back link returns
to the review page.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import PaymentError
from app.core.logging import get_logger
from app.db.models import SftLead, SftOffer
from app.db.session import get_session
from app.sft import config, leads, offers, payments, receipt
from app.sft.config import SftConfig
from app.templating import templates

log = get_logger(__name__)
router = APIRouter(prefix="/sft", tags=["sft"], include_in_schema=False)

# The course as the review page shows it (the same facts as the bot's FAQ).
DAYS = (("Day 1", "Meet Shero Food Technology"),
        ("Day 2", "Understand the process"),
        ("Day 3", "Ingredients and base preparation"),
        ("Day 4", "Hygiene, packing, labels and allergens"),
        ("Day 5", "Order execution and the daily operating rhythm"))
TOPICS = ("Ingredients", "Storage", "Base preparation", "Hygiene", "Packing", "Labels",
          "Allergens", "Order execution", "Customer handoff")
FACTS = (("5", "days, self-paced"), ("10", "video lessons"), ("52", "minutes of video"),
         ("10", "quizzes"))


def _page(request: Request, state: str, title: str, message: str = "", *, status: int = 200,
          **extra):
    return templates.TemplateResponse(
        request=request, name="order/sft.html", status_code=status,
        context={"state": state, "title": title, "message": message,
                 "contact": _contact(), **extra})


def _message(request: Request, title: str, message: str, *, status: int = 200):
    return _page(request, "message", title, message, status=status)


def _contact() -> str:
    """The WhatsApp chat link for questions (the business number), or ''."""
    number = "".join(ch for ch in str(settings.whatsapp_business_number or "") if ch.isdigit())
    return f"https://wa.me/{number}" if len(number) >= 8 else ""


def _masked(lead: SftLead) -> str:
    digits = lead.whatsapp_number or ""
    return f"••• {digits[-4:]}" if len(digits) >= 4 else ""


@router.get("/enrol/{token}", response_class=HTMLResponse, name="sft_enrol")
async def enrol(token: str, request: Request, session: AsyncSession = Depends(get_session)):
    lead = await leads.by_token(session, token)
    if lead is None:
        return _message(request, "Link not found",
                        "This enrollment link isn't valid. Message us on WhatsApp and we'll send "
                        "you a new one.", status=404)
    cfg = await config.load(session)
    if lead.paid_at:
        return await _enrolled(request, session, lead, cfg)
    await payments.clicked(session, lead)
    price = await offers.price(session, lead, cfg)
    test = payments.test_checkout(lead, cfg)
    return _page(
        request, "review", "Enroll in Shero Food Training",
        first=leads.first(lead) if (lead.name or lead.profile_name) else "",
        masked=_masked(lead), test=test, price=price, cfg=cfg,
        fee=cfg.money(price.fee), total=cfg.money(price.amount),
        saving=cfg.money(price.saving) if price.discounted else "",
        offer_name=(price.offer.code or price.offer.title) if price.discounted else "",
        pay_url=f"/sft/enrol/{token}/pay", test_pay_url=f"/sft/enrol/{token}/test-paid",
        brochure=cfg.brochure_link(), days=DAYS, topics=TOPICS, facts=FACTS)


@router.get("/enrol/{token}/pay", name="sft_enrol_pay")
async def pay(token: str, request: Request, session: AsyncSession = Depends(get_session)):
    """The review page's Pay: Stripe's secure checkout at the lead's price."""
    lead = await leads.by_token(session, token)
    if lead is None:
        return _message(request, "Link not found", "This enrollment link isn't valid.",
                        status=404)
    cfg = await config.load(session)
    if lead.paid_at or payments.test_checkout(lead, cfg):
        return RedirectResponse(f"/sft/enrol/{token}", status_code=303)
    try:
        url = await payments.checkout_url(session, lead, cfg)
    except PaymentError as exc:
        log.error("sft_enrol_page_failed", error=str(exc))
        return _message(request, "Please try again",
                        "We couldn't open the secure checkout just now. Please try again in a "
                        "minute, or message us on WhatsApp.", status=502)
    return RedirectResponse(url, status_code=303)


@router.post("/enrol/{token}/test-paid", response_class=HTMLResponse)
async def test_paid(token: str, request: Request, session: AsyncSession = Depends(get_session)):
    lead = await leads.by_token(session, token)
    cfg = await config.load(session)
    if lead is None or not payments.test_checkout(lead, cfg):
        return _message(request, "Not available", "This page is for test payments only.",
                        status=404)
    await payments.mark_paid(session, lead, cfg, amount=None, currency=cfg.currency,
                             test=True)
    return await _enrolled(request, session, lead, cfg)


@router.get("/enrol/{token}/done", response_class=HTMLResponse)
async def done(token: str, request: Request, session: AsyncSession = Depends(get_session)):
    lead = await leads.by_token(session, token)
    if lead is None:
        return _message(request, "Link not found", "This enrollment link isn't valid.",
                        status=404)
    cfg = await config.load(session)
    if await payments.confirm_from_stripe(session, lead, cfg):
        return await _enrolled(request, session, lead, cfg)
    return _page(request, "pending", "Confirming your payment",
                 "We're confirming your payment with Stripe. This page updates by itself, and "
                 "you'll get a WhatsApp message as soon as it's done (usually within a minute).")


async def _enrolled(request: Request, session: AsyncSession, lead: SftLead, cfg: SftConfig):
    title = None
    if lead.offer_id:
        offer = await session.get(SftOffer, lead.offer_id)
        title = offer.title if offer else None
    return _page(
        request, "enrolled", "You're enrolled",
        first=leads.first(lead) if (lead.name or lead.profile_name) else "",
        test=lead.test_payment, course_url=cfg.course_url,
        amount=_amount(lead, cfg), number=receipt.number(lead),
        paid_on=receipt.when(lead.paid_at, cfg), offer_name=lead.offer_code or title or "",
        receipt_url=f"/sft/receipt/{lead.pay_token}/{receipt.filename(lead)}",
        masked=_masked(lead))


def _amount(lead: SftLead, cfg: SftConfig) -> str:
    if lead.amount_paid is None:
        return ""
    if (lead.currency or cfg.currency).lower() == "usd":
        return cfg.money(lead.amount_paid)
    return f"{lead.amount_paid:,.2f} {lead.currency}"


@router.get("/receipt/{token}/{name}", name="sft_receipt")
async def receipt_pdf(token: str, name: str, session: AsyncSession = Depends(get_session)):
    """The enrollment receipt: the copy saved at payment, else drawn now."""
    found = receipt.stored(token, name)
    if found is None:
        lead = await leads.by_token(session, token) if receipt.TOKEN.fullmatch(token) else None
        if lead is None or lead.paid_at is None or name != receipt.filename(lead):
            return Response("Receipt not found.", status_code=404, media_type="text/plain")
        cfg = await config.load(session)
        offer = await session.get(SftOffer, lead.offer_id) if lead.offer_id else None
        found = receipt.save(lead, cfg, offer_title=offer.title if offer else None)
    return FileResponse(found, media_type="application/pdf", filename=name,
                        content_disposition_type="inline",
                        headers={"Cache-Control": "private, max-age=3600",
                                 "X-Robots-Tag": "noindex"})

