"""`GET /receipt/<token>`: the paid invoice as a PDF (services/receipts.py)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Customer, Order, Outlet, PaymentStatus
from app.db.session import get_session
from app.services import receipts

log = get_logger(__name__)
router = APIRouter(tags=["receipts"])


@router.get("/receipt/{token}")
async def download_receipt(token: str, session: AsyncSession = Depends(get_session)):
    order_number = receipts.read_token(token)
    if order_number is None:
        raise HTTPException(status_code=404, detail="Invoice not found.")
    order = (await session.execute(
        select(Order).where(Order.order_number == order_number))).scalar_one_or_none()
    # One answer for a missing and an unpaid order, so the link says nothing.
    if order is None or order.payment_status != str(PaymentStatus.PAID):
        raise HTTPException(status_code=404, detail="Invoice not found.")
    customer = await session.get(Customer, order.customer_id)
    outlet = await session.get(Outlet, order.outlet_id) if order.outlet_id else None
    if customer is None:
        raise HTTPException(status_code=404, detail="Invoice not found.")
    try:
        body = receipts.build_pdf(order, customer, outlet)
    except Exception as exc:
        log.error("receipt_render_failed", order_number=order_number, error=str(exc))
        raise HTTPException(status_code=500, detail="The invoice could not be made.") from exc
    log.info("receipt_downloaded", order_number=order_number)
    return Response(body, media_type="application/pdf", headers={
        "Content-Disposition": f'inline; filename="{receipts.filename(order)}"',
        "Cache-Control": "private, no-store",
    })
