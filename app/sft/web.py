"""The pages behind SFT's Pay button: /sft/enrol/<token>.

    GET  /sft/enrol/<token>             a live Stripe checkout (or the test page)
    POST /sft/enrol/<token>/test-paid   the test page's button: paid, nobody charged
    GET  /sft/enrol/<token>/done        back from Stripe: "you're enrolled"

The token is the lead's own random pay token, so a link identifies one lead
and nothing else (no number or name in the URL).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import PaymentError
from app.core.logging import get_logger
from app.db.session import get_session
from app.sft import config, leads, payments
from app.templating import templates

log = get_logger(__name__)
router = APIRouter(prefix="/sft", tags=["sft"], include_in_schema=False)


def _page(request: Request, title: str, message: str, *, status: int = 200, **extra):
    return templates.TemplateResponse(
        request=request, name="order/sft.html", status_code=status,
        context={"title": title, "message": message, **extra})


@router.get("/enrol/{token}", response_class=HTMLResponse, name="sft_enrol")
async def enrol(token: str, request: Request, session: AsyncSession = Depends(get_session)):
    lead = await leads.by_token(session, token)
    if lead is None:
        return _page(request, "Link not found",
                     "This enrollment link isn't valid. Message us on WhatsApp and we'll send "
                     "you a new one.", status=404)
    cfg = await config.load(session)
    if lead.paid_at:
        return _page(request, "You're enrolled 🎉",
                     "Your payment is confirmed. Your course details are in your WhatsApp chat.",
                     course_url=cfg.course_url)
    await payments.clicked(session, lead)
    if payments.test_checkout(lead, cfg):
        return _page(request, "Test payment",
                     f"Shero Food Training (SFT): {cfg.fee_label()}. This is a test checkout: "
                     "nobody is charged.", test_pay_url=f"/sft/enrol/{token}/test-paid",
                     fee=cfg.fee_label())
    try:
        url = await payments.checkout_url(session, lead, cfg)
    except PaymentError as exc:
        log.error("sft_enrol_page_failed", error=str(exc))
        return _page(request, "Please try again",
                     "We couldn't open the secure checkout just now. Please try the link again "
                     "in a minute, or message us on WhatsApp.", status=502)
    return RedirectResponse(url, status_code=303)


@router.post("/enrol/{token}/test-paid", response_class=HTMLResponse)
async def test_paid(token: str, request: Request, session: AsyncSession = Depends(get_session)):
    lead = await leads.by_token(session, token)
    cfg = await config.load(session)
    if lead is None or not payments.test_checkout(lead, cfg):
        return _page(request, "Not available", "This page is for test payments only.",
                     status=404)
    await payments.mark_paid(session, lead, cfg, amount=cfg.course_fee, currency=cfg.currency,
                             test=True)
    return _page(request, "Test payment done ✅",
                 "Marked as paid (a test: nobody was charged). Check WhatsApp for the "
                 "enrollment message.")


@router.get("/enrol/{token}/done", response_class=HTMLResponse)
async def done(token: str, request: Request, session: AsyncSession = Depends(get_session)):
    lead = await leads.by_token(session, token)
    if lead is None:
        return _page(request, "Link not found", "This enrollment link isn't valid.", status=404)
    cfg = await config.load(session)
    paid = await payments.confirm_from_stripe(session, lead, cfg)
    if paid:
        return _page(request, "You're enrolled 🎉",
                     "Thank you! Your payment is confirmed. We've sent your next steps to your "
                     "WhatsApp chat.", course_url=cfg.course_url)
    return _page(request, "Thank you!",
                 "We're confirming your payment with Stripe. You'll get a WhatsApp message as "
                 "soon as it's done (usually within a minute).")
