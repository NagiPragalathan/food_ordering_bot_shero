"""Admin loyalty policy, customer balances, history and audited adjustments."""
from __future__ import annotations
import json
import uuid
from fastapi import APIRouter, Depends, Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.admin.deps import redirect, render, require_admin
from app.db.models import AdminUser, Customer, LoyaltyAccount, Outlet
from app.db.session import get_session
from app.services import loyalty

router = APIRouter(prefix="/loyalty", tags=["admin"])

@router.get("", name="admin_loyalty")
async def index(request: Request, q: str = "", session: AsyncSession = Depends(get_session),
                current_user: AdminUser = Depends(require_admin)):
    rules = await loyalty.policy(session)
    values = rules.model_dump(mode="json")
    fields = []
    for key, field in loyalty.Policy.model_fields.items():
        if key in {"tiers", "kitchen_ids", "excluded_items"}:
            continue
        value = values[key]
        fields.append({"key": key, "label": field.description, "value": value,
                       "kind": "checkbox" if isinstance(value, bool) else
                               "text" if key in {"name", "currency"} else "number"})
    query = select(Customer, LoyaltyAccount).outerjoin(LoyaltyAccount, LoyaltyAccount.customer_id == Customer.id)
    if q.strip():
        term = f"%{q.strip()}%"
        query = query.where(or_(Customer.name.ilike(term), Customer.whatsapp_number.ilike(term)))
    rows = (await session.execute(query.order_by(Customer.created_at.desc()).limit(50))).all()
    return render(request, "admin/loyalty.html", {
        "current_user": current_user, "rules": rules, "fields": fields, "rows": rows, "query": q,
        "tiers_json": json.dumps(values["tiers"], indent=2),
        "kitchens": list((await session.execute(select(Outlet).order_by(Outlet.name))).scalars())})

@router.post("", name="admin_loyalty_save")
async def save(request: Request, session: AsyncSession = Depends(get_session),
               current_user: AdminUser = Depends(require_admin)):
    form = await request.form()
    try:
        data = {}
        for key, field in loyalty.Policy.model_fields.items():
            if field.annotation is bool:
                data[key] = form.get(key) == "1"
            elif key == "tiers":
                data[key] = json.loads(str(form.get(key) or "[]"))
            elif key == "kitchen_ids":
                data[key] = list(form.getlist(key))
            elif key == "excluded_items":
                data[key] = [x.strip() for x in str(form.get(key) or "").replace(",", "\n").splitlines() if x.strip()]
            else:
                data[key] = form.get(key)
        await loyalty.save_policy(session, data, current_user.email)
    except (ValueError, TypeError) as exc:
        return redirect(str(request.url_for("admin_loyalty")), flash=("error", f"Settings were not saved: {exc}"))
    return redirect(str(request.url_for("admin_loyalty")), flash=("success", "Loyalty rules saved. Existing payment links keep their agreed rules."))

@router.get("/{customer_id}", name="admin_loyalty_wallet")
async def wallet(request: Request, customer_id: uuid.UUID, session: AsyncSession = Depends(get_session),
                 current_user: AdminUser = Depends(require_admin)):
    customer = await session.get(Customer, customer_id)
    if customer is None:
        return redirect(str(request.url_for("admin_loyalty")), flash=("error", "Customer not found."))
    return render(request, "admin/loyalty_wallet.html", {
        "current_user": current_user, "customer": customer,
        "info": await loyalty.overview(session, customer_id), "request_id": str(uuid.uuid4())})

@router.post("/{customer_id}/adjust", name="admin_loyalty_adjust")
async def adjust(request: Request, customer_id: uuid.UUID, session: AsyncSession = Depends(get_session),
                 current_user: AdminUser = Depends(require_admin)):
    form = await request.form()
    try:
        if await session.get(Customer, customer_id) is None:
            raise ValueError("Customer not found")
        await loyalty.adjust(session, customer_id, int(str(form.get("points") or "0")),
                             note=str(form.get("note") or ""), actor=current_user.email,
                             request_id=form.get("request_id"))
    except (ValueError, loyalty.LoyaltyError) as exc:
        return redirect(str(request.url_for("admin_loyalty_wallet", customer_id=customer_id)), flash=("error", str(exc)))
    return redirect(str(request.url_for("admin_loyalty_wallet", customer_id=customer_id)), flash=("success", "Points adjustment recorded."))
