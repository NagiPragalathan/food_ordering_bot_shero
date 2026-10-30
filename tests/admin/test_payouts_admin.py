"""The Payouts page: split rules, what each kitchen is owed, and payments."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin import router
from app.admin.deps import require_admin
from app.db.models import AdminUser, Order, OrderStage, PartnerPayout, PaymentStatus
from app.db.session import get_session
from app.services import payouts

D = Decimal
DRUMSTICK = "chettinad-sambar-drumstick-sambar"   # $12.50, PPP $8.16


@pytest.fixture
def admin(session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_admin] = lambda: AdminUser(
        email="ops@shero.test", name="Ops", password_hash="x", is_active=True)
    return TestClient(app, follow_redirects=False)


async def order(session, customer, outlet, number, quantity=8, *, stage=OrderStage.DELIVERED,
                paid_at=None):
    food = D("12.50") * quantity
    row = Order(order_number=number, customer_id=customer.id, outlet_id=outlet.id,
                items=[{"retailer_id": DRUMSTICK, "name": "Drumstick Sambar", "quantity": quantity,
                        "unit_price": "12.50", "line_total": str(food)}],
                dish_total=food, delivery_fee=D("8.00"), total=food + D("8.00"),
                payment_status=PaymentStatus.PAID, stage=stage,
                paid_at=paid_at or datetime.now(timezone.utc))
    session.add(row)
    await session.flush()
    return row


def flash(response) -> str:
    """The message a redirect carries (a signed cookie), good enough to search."""
    return response.headers.get("set-cookie", "")


async def test_the_page_and_its_tabs(admin, session, customer, outlet, menu):
    await order(session, customer, outlet, "SHO-PAY-1")
    page = admin.get("/admin/payouts")
    assert page.status_code == 200
    for text in ("Customers paid", "Kitchens&#39; share", "By kitchen", "Shero Edison",
                 "SHO-PAY-1", "$108.00", "$65.00", "$35.00", "Record payment",
                 "Use today's split for these orders"):
        assert text in page.text, text
    orders = admin.get("/admin/orders").text
    assert '/admin/payouts"' in orders                                     # in the sidebar
    assert "Kitchen $65.00 · Shero $35.00" in orders                       # on each paid order

    rules = admin.get("/admin/payouts?tab=rules")
    assert rules.status_code == 200
    for text in ("Kitchen&#39;s share", "Coupons and loyalty points", "When a kitchen is owed",
                 "Kitchens with their own split", "Try it", "What the saved split gives",
                 "4 of 4 dishes have a PPP"):
        assert text in rules.text, text
    history = admin.get("/admin/payouts?tab=history")
    assert history.status_code == 200 and "No payments recorded yet" in history.text

    # Filters: a kitchen, a status, dates that miss the order.
    assert "SHO-PAY-1" in admin.get(f"/admin/payouts?kitchen={outlet.id}&status=due").text
    assert "SHO-PAY-1" not in admin.get("/admin/payouts?status=paid_out").text
    old = admin.get("/admin/payouts?period=custom&start=2020-01-01&end=2020-01-31")
    assert old.status_code == 200 and "SHO-PAY-1" not in old.text and "01 Jan – 31 Jan 2020" in old.text
    assert admin.get("/admin/payouts?period=nonsense&kitchen=bad&page=x").status_code in (200, 422)


async def test_saving_the_split(admin, session, outlet, far_outlet):
    empty = admin.get("/admin/payouts")
    assert empty.status_code == 200 and "No paid orders in these dates." in empty.text
    form = {"tab": "rules", "basis": "dish_price", "kitchen_percent": "60", "discounts": "shared",
            "payable_after": "paid", "rule_kitchen": [str(outlet.id), str(far_outlet.id)],
            "rule_basis": ["", "percent"], "rule_percent": ["", "72.5"]}
    response = admin.post("/admin/payouts/rules", data=form)
    assert response.status_code == 303 and "tab=rules" in response.headers["location"]
    rules = await payouts.rules(session)
    assert (rules.basis, rules.kitchen_percent, rules.discounts, rules.payable_after) == (
        "dish_price", D("60.00"), "shared", "paid")
    assert rules.rule_for(far_outlet.id) == ("percent", D("72.50"))
    assert rules.rule_for(outlet.id) == ("dish_price", D("60.00"))

    page = admin.get("/admin/payouts?tab=rules").text
    assert 'value="72.5"' in page and "Partner price (PPP); 60% for a dish without one" in page

    admin.post("/admin/payouts/rules", data={**form, "kitchen_percent": "160"})
    assert (await payouts.rules(session)).kitchen_percent == D("60.00")    # kept


async def test_recording_and_undoing_a_payment(admin, session, customer, outlet, menu):
    a = await order(session, customer, outlet, "SHO-PAY-1")
    b = await order(session, customer, outlet, "SHO-PAY-2", 2)
    await order(session, customer, outlet, "SHO-PAY-3", stage=OrderStage.SENT_TO_KITCHEN)
    form = {"pay_kitchen": str(outlet.id), "period": "all", "paid_on": "2026-10-30",
            "reference": "Zelle 4821", "note": "October"}

    # The amount changed since the page was drawn: nothing is recorded.
    stale = admin.post("/admin/payouts/pay", data={**form, "expected": "10.00"})
    assert stale.status_code == 303 and a.payout_id is None

    page = admin.get("/admin/payouts?period=all").text
    assert 'name="expected" value="81.25"' in page                       # 65 + 16.25
    done = admin.post("/admin/payouts/pay", data={**form, "expected": "81.25"})
    assert done.status_code == 303
    payout = (await session.execute(PartnerPayout.__table__.select())).one()
    assert (payout.amount, payout.order_count, payout.reference) == (D("81.25"), 2, "Zelle 4821")
    assert a.payout_id == b.payout_id == payout.id

    history = admin.get("/admin/payouts?tab=history").text
    assert "Zelle 4821" in history and "$81.25" in history and "2 order(s)" in history
    csv = admin.get(f"/admin/payouts/{payout.id}/statement.csv")
    assert csv.status_code == 200 and "SHO-PAY-1" in csv.text and "SHO-PAY-3" not in csv.text
    assert "attachment" in csv.headers["content-disposition"]

    assert admin.post(f"/admin/payouts/{payout.id}/undo", data={}).status_code == 303
    assert a.payout_id is None and b.payout_id is None
    assert "Undone by ops@shero.test" in admin.get("/admin/payouts?tab=history").text


async def test_setting_an_order_amount_and_copying_the_split(admin, session, customer, outlet, menu):
    row = await order(session, customer, outlet, "SHO-PAY-1")
    response = admin.post("/admin/payouts/orders/sho-pay-1",
                          data={"amount": "70", "note": "extra pack", "period": "all"})
    assert response.status_code == 303 and "period=all" in response.headers["location"]
    assert row.payout_split["manual"]["amount"] == "70.00"
    page = admin.get("/admin/payouts").text
    assert "set by hand" in page and "extra pack" in page

    admin.post("/admin/payouts/orders/SHO-PAY-1", data={"amount": "999"})
    assert row.payout_split["manual"]["amount"] == "70.00"                 # refused
    admin.post("/admin/payouts/orders/SHO-PAY-1", data={"clear": "1"})
    assert "manual" not in row.payout_split
    assert admin.post("/admin/payouts/orders/NOPE", data={}).status_code == 303

    other = await order(session, customer, outlet, "SHO-PAY-2")
    assert not payouts.is_locked(other)
    assert admin.post("/admin/payouts/apply-rules", data={"period": "all"}).status_code == 303
    assert payouts.is_locked(other)


async def test_the_statement_for_a_view(admin, session, customer, outlet, far_outlet, menu):
    await order(session, customer, outlet, "SHO-PAY-1")
    await order(session, customer, far_outlet, "SHO-PAY-2")
    await order(session, customer, outlet, "SHO-PAY-OLD",
                paid_at=datetime.now(timezone.utc) - timedelta(days=400))
    csv = admin.get(f"/admin/payouts/statement.csv?period=last_30&kitchen={outlet.id}")
    assert csv.status_code == 200
    assert "SHO-PAY-1" in csv.text and "SHO-PAY-2" not in csv.text and "SHO-PAY-OLD" not in csv.text
    assert "shero-edison" in csv.headers["content-disposition"]
    everything = admin.get("/admin/payouts/statement.csv?period=all").text
    assert all(n in everything for n in ("SHO-PAY-1", "SHO-PAY-2", "SHO-PAY-OLD"))
