"""The read-only integration API (app/api/routes/integration.py): kitchens,
cuisines, the menu and orders with their dishes, behind each business's own
X-Api-Key, for DST India's CRM."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import integration
from app.core import tenants
from app.core.config import settings_for
from app.db.models import Order
from app.db.session import get_session

KEY = "shk_test_key_for_the_integration_api"


@pytest.fixture
def api(session, monkeypatch):
    monkeypatch.setattr(settings_for(tenants.US), "integration_api_key", KEY)
    app = FastAPI()
    app.include_router(integration.router)
    app.add_middleware(tenants.TenantPathMiddleware)
    app.dependency_overrides[get_session] = lambda: session
    client = TestClient(app)
    client.headers["X-Api-Key"] = KEY
    return client


async def _order(session, customer, outlet, menu, *, number="SHO-261123-AB12X", paid=True,
                 created=None) -> Order:
    lines = [{"retailer_id": "chettinad-sambar-drumstick-sambar", "name": "Drumstick Sambar",
              "quantity": 2, "unit_price": "12.50"},
             {"retailer_id": "kerala-sambar-appam", "name": "Appam", "quantity": 1,
              "unit_price": "3.25"}]
    order = Order(order_number=number, customer_id=customer.id, outlet_id=outlet.id, items=lines,
                  dish_total=Decimal("28.25"), delivery_fee=Decimal("4.00"),
                  tax=Decimal("0"), total=Decimal("32.25"), currency="USD",
                  delivery_address="12 Maple Street", postal_code="08820",
                  slot_label="Wed 25 Nov, 7:00 PM - 8:00 PM",
                  payment_status="paid" if paid else "link_sent",
                  stage="Paid & Slot Booked" if paid else "Pending Payment",
                  paid_at=datetime.now(timezone.utc) if paid else None,
                  stripe_payment_intent_id="pi_123" if paid else None)
    if created:
        order.created_at = created
    session.add(order)
    await session.flush()
    return order


def test_no_key_set_means_off(session, monkeypatch):
    monkeypatch.setattr(settings_for(tenants.US), "integration_api_key", "")
    app = FastAPI()
    app.include_router(integration.router)
    app.dependency_overrides[get_session] = lambda: session
    answer = TestClient(app).get("/api/v1/menu", headers={"X-Api-Key": "anything"})
    assert answer.status_code == 503


def test_a_wrong_or_missing_key_is_refused(api):
    assert api.get("/api/v1/menu", headers={"X-Api-Key": "nope"}).status_code == 401
    del api.headers["X-Api-Key"]
    assert api.get("/api/v1/orders").status_code == 401


def test_each_business_has_its_own_key(api, monkeypatch):
    # DST India has no key of its own yet: Shero US's key opens nothing there.
    monkeypatch.setattr(settings_for(tenants.DST), "integration_api_key", "")
    assert api.get("/dst/api/v1/menu").status_code == 503
    monkeypatch.setattr(settings_for(tenants.DST), "integration_api_key", "shk_dst_only")
    assert api.get("/dst/api/v1/menu").status_code == 401
    assert api.get("/dst/api/v1/menu", headers={"X-Api-Key": "shk_dst_only"}).status_code == 200


async def test_kitchens(api, outlet):
    body = api.get("/api/v1/kitchens").json()
    kitchen = body["data"][0]
    assert body["total"] == 1 and kitchen["code"] == "EDISON"
    assert kitchen["address"]["city"] == "Edison" and kitchen["cuisines"] == ["andhra", "kerala"]
    assert "notes" not in kitchen


async def test_cuisines_come_with_their_categories(api, menu):
    body = api.get("/api/v1/cuisines").json()
    assert [c["slug"] for c in body["data"]] == ["chettinad", "kerala"]
    chettinad = body["data"][0]
    assert [(c["name"], c["item_count"]) for c in chettinad["categories"]] == [
        ("Sambar", 2), ("Rasam", 1)]


async def test_the_menu_with_cuisine_category_and_price(api, menu):
    body = api.get("/api/v1/menu").json()
    assert body["total"] == 4 and body["next_offset"] is None
    dish = next(d for d in body["data"] if d["sku"] == "chettinad-sambar-drumstick-sambar")
    assert dish["name"] == "Drumstick Sambar" and dish["price"] == "12.50"
    assert dish["cuisine"]["slug"] == "chettinad" and dish["category"]["name"] == "Sambar"
    assert dish["pack_size"] == "16 oz pack" and "cost_price" not in dish     # internal cost stays in
    kerala = api.get("/api/v1/menu", params={"cuisine": "kerala"}).json()
    assert [d["name"] for d in kerala["data"]] == ["Appam"]
    paged = api.get("/api/v1/menu", params={"limit": 3}).json()
    assert len(paged["data"]) == 3 and paged["next_offset"] == 3
    found = api.get("/api/v1/menu", params={"q": "rasam"}).json()
    assert [d["name"] for d in found["data"]] == ["Tomato Rasam"]


async def test_orders_with_their_dishes(api, session, customer, outlet, menu):
    await _order(session, customer, outlet, menu)
    await _order(session, customer, outlet, menu, number="SHO-261123-CART1", paid=False)
    body = api.get("/api/v1/orders").json()
    assert body["total"] == 1                                   # paid ones by default
    order = body["data"][0]
    assert order["number"] == "SHO-261123-AB12X" and order["payment_status"] == "paid"
    assert order["kitchen"] == "Shero Edison" and order["kitchen_code"] == "EDISON"
    assert order["total"] == "32.25" and order["currency"] == "USD"
    assert order["items"][0] == {"name": "Drumstick Sambar",
                                 "sku": "chettinad-sambar-drumstick-sambar", "quantity": 2,
                                 "rate": "12.50", "amount": "25.00", "cuisine": "Chettinad",
                                 "category": "Sambar"}
    assert order["customer"]["phone"] == "17325550142" and order["customer"]["name"] == "Asha Menon"
    assert order["paid_at"].endswith("Z") and order["test_payment"] is False
    assert api.get("/api/v1/orders", params={"payment_status": "all"}).json()["total"] == 2
    assert api.get("/api/v1/orders", params={"payment_status": "bogus"}).status_code == 422


async def test_order_filters(api, session, customer, outlet, menu):
    old = datetime.now(timezone.utc) - timedelta(days=10)
    await _order(session, customer, outlet, menu, number="SHO-OLD", created=old)
    await _order(session, customer, outlet, menu, number="SHO-NEW")
    today = datetime.now(timezone.utc).date().isoformat()
    assert [o["number"] for o in api.get("/api/v1/orders", params={"from": today}).json()["data"]] \
        == ["SHO-NEW"]
    assert api.get("/api/v1/orders", params={"kitchen": "edison"}).json()["total"] == 2
    assert api.get("/api/v1/orders", params={"kitchen": "NOPE"}).status_code == 404
    assert api.get("/api/v1/orders", params={"phone": "+1 (732) 555-0142"}).json()["total"] == 2
    assert api.get("/api/v1/orders", params={"from": "23-11-2026"}).status_code == 422


async def test_one_order(api, session, customer, outlet, menu):
    await _order(session, customer, outlet, menu)
    body = api.get("/api/v1/orders/sho-261123-ab12x").json()
    assert body["data"]["number"] == "SHO-261123-AB12X" and len(body["data"]["items"]) == 2
    assert api.get("/api/v1/orders/SHO-NONE").status_code == 404


async def test_the_settings_card_makes_a_key_shown_once(session, monkeypatch):
    from app.admin import router as admin_router
    from app.admin.deps import require_admin
    from app.db.models import AdminUser
    from app.services import settings_store

    monkeypatch.setattr(settings_for(tenants.US), "settings_encryption_key",
                        "kzX0ZqkP8m7G0fq2pX4Q1v9w3Jt6Yb5Hs8Lc2Nd4Re0=")
    monkeypatch.setattr(settings_for(tenants.US), "integration_api_key", "")
    monkeypatch.setattr(settings_store, "_applied", settings_store.tenants.PerTenant(set))
    app = FastAPI()
    app.include_router(admin_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_admin] = lambda: AdminUser(
        email="ops@shero.test", name="Ops", password_hash="x", is_active=True)
    admin = TestClient(app, follow_redirects=False)
    page = admin.get("/admin/settings").text
    assert "data-integration-card" in page and "Switch on: generate a key" in page
    made = admin.post("/admin/settings/integration-key/generate")
    assert made.status_code == 200 and "data-integration-new-key" in made.text
    key = settings_for(tenants.US).integration_api_key
    assert key.startswith("shk_") and key in made.text
    assert key not in admin.get("/admin/settings").text          # never shown again
    admin.post("/admin/settings/integration-key/off")
    assert settings_for(tenants.US).integration_api_key == ""
