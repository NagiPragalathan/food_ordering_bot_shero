"""Real admin pages: settings, wallet history, adjustments and authentication."""
import json
import uuid
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.admin import router
from app.admin.deps import require_admin
from app.db.session import get_session
from app.db.models import AdminUser
from app.services import loyalty

@pytest.fixture
def admin(session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_admin] = lambda: AdminUser(email="ops@test",name="Ops",password_hash="x",is_active=True)
    return TestClient(app, follow_redirects=False)

def form(**changes):
    data = loyalty.Policy(enabled=True).model_dump(mode="json")
    data.update(changes)
    for key in list(data):
        if isinstance(data[key], bool):
            if data[key]: data[key] = "1"
            else: del data[key]
    data["tiers"] = json.dumps(data["tiers"])
    data["excluded_items"] = "\n".join(data["excluded_items"])
    return data

async def test_pages_and_program_save(admin, session, customer, outlet):
    page = admin.get("/admin/loyalty")
    assert page.status_code == 200
    assert "Program rules" in page.text and "Customer wallets" in page.text
    assert customer.whatsapp_number in page.text
    # Not in the sidebar (url_for renders absolute links, so match the path's end).
    offers = admin.get("/admin/promotions").text
    assert '/admin/promotions"' in offers and '/admin/loyalty"' not in offers
    result = admin.post("/admin/loyalty", data=form(points_per_currency="2", first_order_bonus=50,
                        kitchen_ids=[str(outlet.id)], excluded_items=["not-eligible"]))
    assert result.status_code == 303
    rules = await loyalty.policy(session)
    assert rules.enabled and rules.points_per_currency == 2 and rules.first_order_bonus == 50
    assert rules.kitchen_ids == [outlet.id] and rules.excluded_items == ["not-eligible"]
    wallet = admin.get(f"/admin/loyalty/{customer.id}")
    assert wallet.status_code == 200 and "0 available points" in wallet.text

async def test_bad_rules_leave_previous_policy_intact(admin, session):
    admin.post("/admin/loyalty", data=form(points_per_currency="2"))
    admin.post("/admin/loyalty", data=form(points_per_currency="-1"))
    assert (await loyalty.policy(session)).points_per_currency == 2

async def test_adjustments_history_and_repeat_submission(admin, session, customer):
    payload = {"points":"200", "note":"Service goodwill", "request_id":str(uuid.uuid4())}
    for _ in range(2):
        assert admin.post(f"/admin/loyalty/{customer.id}/adjust", data=payload).status_code == 303
    page = admin.get(f"/admin/loyalty/{customer.id}")
    assert "200 available points" in page.text and "Service goodwill" in page.text
    assert "ops@test" in page.text
    assert (await loyalty.account(session, customer.id)).balance == 200

async def test_admin_is_required(session):
    from app.admin.deps import NotAuthenticated
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    from fastapi.responses import JSONResponse
    @app.exception_handler(NotAuthenticated)
    async def denied(request, exc):
        return JSONResponse({"error":"login required"},status_code=401)
    client = TestClient(app)
    assert client.get("/admin/loyalty").status_code == 401
    assert client.post("/admin/loyalty",data=form()).status_code == 401
