"""Real admin pages: settings, wallet history, adjustments and authentication."""
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
    """The page's form as a browser posts it: rows of repeated inputs for
    tiers and dish bonuses, one value per ticked option."""
    data = loyalty.Policy(enabled=True).model_dump(mode="json")
    data.update(changes)
    tiers, dishes = data.pop("tiers"), data.pop("dish_bonuses")
    data["tier_name"] = [t["name"] for t in tiers]
    data["tier_orders"] = [str(t["orders"]) for t in tiers]
    data["tier_multiplier"] = [str(t["multiplier"]) for t in tiers]
    data["dish_bonus_item"] = [d["retailer_id"] for d in dishes]
    data["dish_bonus_points"] = [str(d["points"]) for d in dishes]
    for key in list(data):
        if data[key] is None:
            data[key] = ""
        elif isinstance(data[key], bool):
            if data[key]: data[key] = "1"
            else: del data[key]
    return data

async def test_pages_and_program_save(admin, session, customer, outlet, menu):
    page = admin.get("/admin/loyalty")
    assert page.status_code == 200
    for heading in ("Earning points", "Bonus points", "Bonus-points promotion", "Dish bonuses",
                    "Membership tiers", "Where points apply", "What the saved rules give",
                    "Customer wallets", "Reset every balance to zero"):
        assert heading in page.text, heading
    assert customer.whatsapp_number in page.text and "Drumstick Sambar" in page.text
    # In the sidebar (url_for renders absolute links, so match the path's end).
    assert '/admin/loyalty"' in admin.get("/admin/promotions").text

    result = admin.post("/admin/loyalty", data=form(
        points_per_currency="2", first_order_bonus=50, kitchen_ids=[str(outlet.id)],
        excluded_items=["not-eligible"], big_order_threshold="40", big_order_bonus=30,
        promo_multiplier="2", promo_days=["sat", "sun"], promo_starts_on="2026-11-01",
        tiers=[{"name": "Gold", "orders": 3, "multiplier": "2"}],
        dish_bonuses=[{"retailer_id": "chettinad-sambar-drumstick-sambar", "points": 20}]))
    assert result.status_code == 303
    rules = await loyalty.policy(session)
    assert rules.enabled and rules.points_per_currency == 2 and rules.first_order_bonus == 50
    assert rules.kitchen_ids == [outlet.id] and rules.excluded_items == ["not-eligible"]
    assert rules.big_order_bonus == 30 and rules.promo_multiplier == 2 and rules.promo_days == ["sat", "sun"]
    assert str(rules.promo_starts_on) == "2026-11-01" and rules.promo_ends_on is None
    assert [(t.name, t.orders) for t in rules.tiers] == [("Gold", 3)]
    assert [(d.retailer_id, d.points) for d in rules.dish_bonuses] == [("chettinad-sambar-drumstick-sambar", 20)]
    assert rules.currency == "USD"                      # not on the page, kept as saved

    page = admin.get("/admin/loyalty").text
    # $20 of food, first order: 20 x 2 + 50 = 90; x2 promotion: 130.
    assert "90 <span class=\"text-stone-500\">(130)</span>" in page
    wallet = admin.get(f"/admin/loyalty/{customer.id}")
    assert wallet.status_code == 200 and "0 available points" in wallet.text

async def test_bad_rules_leave_previous_policy_intact(admin, session):
    admin.post("/admin/loyalty", data=form(points_per_currency="2"))
    admin.post("/admin/loyalty", data=form(points_per_currency="-1"))
    admin.post("/admin/loyalty", data=form(promo_starts_on="2026-11-10", promo_ends_on="2026-11-01"))
    admin.post("/admin/loyalty", data=form(big_order_bonus=30, big_order_threshold="0"))
    rules = await loyalty.policy(session)
    assert rules.points_per_currency == 2 and rules.promo_starts_on is None and rules.big_order_bonus == 0

async def test_adjustments_history_and_repeat_submission(admin, session, customer):
    payload = {"points":"200", "note":"Service goodwill", "request_id":str(uuid.uuid4())}
    for _ in range(2):
        assert admin.post(f"/admin/loyalty/{customer.id}/adjust", data=payload).status_code == 303
    page = admin.get(f"/admin/loyalty/{customer.id}")
    assert "200 available points" in page.text and "Service goodwill" in page.text
    assert "ops@test" in page.text
    assert (await loyalty.account(session, customer.id)).balance == 200

async def test_reset_every_balance_needs_the_word_reset(admin, session, customer):
    from tests.services.test_loyalty import grant
    await grant(session, customer, 300)

    admin.post("/admin/loyalty/reset-all", data={"confirm": "yes"})
    assert (await loyalty.account(session, customer.id)).balance == 300

    assert admin.post("/admin/loyalty/reset-all",
                      data={"confirm": "reset", "note": "New program"}).status_code == 303
    assert (await loyalty.account(session, customer.id)).balance == 0
    history = admin.get(f"/admin/loyalty/{customer.id}").text
    assert "New program" in history and "-300" in history and "ops@test" in history

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
    assert client.post("/admin/loyalty/reset-all",data={"confirm":"RESET"}).status_code == 401
