"""DST India's admin: the same pages as Shero US under /dst/admin, chosen
with the business tabs, every link staying inside the business."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin import router
from app.admin.deps import require_admin
from app.core import tenants
from app.db.models import AdminUser, DeliverySlot, Order, OrderStage, PaymentStatus
from app.db.session import get_session


@pytest.fixture
def admin(session):
    app = FastAPI()
    app.include_router(router)
    app.add_middleware(tenants.TenantPathMiddleware)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_admin] = lambda: AdminUser(
        email="ops@shero.test", name="Ops", password_hash="x", is_active=True)
    return TestClient(app, follow_redirects=False)


def tabs(html: str) -> list[tuple[str, str, bool]]:
    bar = html.split("data-business-tabs", 1)[1].split("</nav>", 1)[0]
    return [(href, name.strip(), "aria-current" in attrs)
            for href, attrs, name in re.findall(r'<a href="([^"]+)"([^>]*)>([^<]+)</a>', bar)]


def test_both_businesses_have_tabs_and_each_opens_the_same_section(admin):
    us = admin.get("/admin/orders")
    assert us.status_code == 200
    assert tabs(us.text) == [("/admin/orders", "Shero US", True),
                             ("/dst/admin/orders", "DST India", False),
                             ("/admin/sft", "Shero Food Training", False)]

    dst = admin.get("/dst/admin/orders")
    assert dst.status_code == 200
    assert tabs(dst.text) == [("/admin/orders", "Shero US", False),
                              ("/dst/admin/orders", "DST India", True),
                              ("/admin/sft", "Shero Food Training", False)]


def test_every_link_on_a_dst_page_stays_in_dst(admin):
    page = admin.get("/dst/admin/menu").text
    # url_for links are absolute: http://testserver/dst/admin/... (the shared
    # /static/css/admin.css stylesheet is not a page link).
    sidebar = re.findall(r'href="(?:https?://[^/"]+)?(/(?!static/)[^"]*admin[^"]*)"',
                         page.split("data-business-tabs")[0])
    assert len(sidebar) > 10 and all(link.startswith("/dst/admin") for link in sidebar)
    assert "DST India Admin" in page


@pytest.mark.parametrize("section", ["", "menu", "orders", "promotions", "loyalty", "nudges",
                                     "kitchens", "payouts", "deliveries", "customers",
                                     "bot-replies", "settings"])
def test_every_us_page_exists_for_dst_india(admin, section):
    assert admin.get(f"/admin/{section}").status_code == 200
    assert admin.get(f"/dst/admin/{section}").status_code == 200


def test_the_sidebar_is_the_same_for_both(admin):
    def labels(html):
        nav = html.split('aria-label="Business"')[-1] if False else html
        return re.findall(r'</svg>\s*([A-Z][A-Za-z ]+?)\s*(?:<span|</a>)', nav.split("<nav", 2)[1])

    us, dst = labels(admin.get("/admin/").text), labels(admin.get("/dst/admin/").text)
    assert len(us) == len(dst)
    # Only the delivery page is named for who delivers.
    assert [x for x in us if x not in dst] == ["Uber queue"]
    assert [x for x in dst if x not in us] == ["Delivery queue"]


async def test_dst_delivery_queue_lists_orders_for_its_own_riders(admin, session, customer, outlet):
    start = datetime.now(timezone.utc) + timedelta(hours=4)
    slot = DeliverySlot(outlet_id=outlet.id, starts_at=start, ends_at=start + timedelta(hours=1),
                        capacity=5, reserved_count=1)
    session.add(slot)
    await session.flush()
    session.add(Order(order_number="DST-261116-RIDER", customer_id=customer.id,
                      outlet_id=outlet.id, slot_id=slot.id, items=[], item_count=2,
                      dish_total=Decimal("498"), delivery_fee=Decimal("40"),
                      total=Decimal("562.90"), currency="INR", delivery_address="12 Anna Salai",
                      postal_code="600002", payment_status=PaymentStatus.PAID,
                      stage=OrderStage.SENT_TO_KITCHEN))
    await session.flush()

    page = admin.get("/dst/admin/deliveries").text
    assert "Delivery queue" in page and "Uber" not in page.split("<main", 1)[1]
    assert "DST-261116-RIDER" in page and "₹562.90" in page and "600002" in page
    assert "Out for delivery" in page          # the order's next step
    assert '/dst/admin/orders/DST-261116-RIDER/advance"' in page


def test_the_settings_page_shows_each_business_its_own_delivery(admin):
    us, dst = admin.get("/admin/settings").text, admin.get("/dst/admin/settings").text
    assert 'name="UBER_DISPATCH_HOURS_BEFORE"' in us and 'name="OWN_DELIVERY_FEE"' not in us
    assert 'name="OWN_DELIVERY_FEE"' in dst and 'name="UBER_DISPATCH_HOURS_BEFORE"' not in dst


def test_each_dashboard_shows_its_own_whatsapp_number(admin, monkeypatch):
    from app.core.config import settings_for

    monkeypatch.setattr(settings_for(tenants.US), "whatsapp_business_number", "17325550199")
    monkeypatch.setattr(settings_for(tenants.US), "gallabox_channel_id", "us-channel-1")
    monkeypatch.setattr(settings_for(tenants.DST), "whatsapp_business_number", "919360056005")
    monkeypatch.setattr(settings_for(tenants.DST), "gallabox_channel_id", "6af30a65a7aa4cd16b22569a")

    def banner(path):
        return admin.get(path).text.split("data-channel", 1)[1].split("</div>", 1)[0]

    us, dst = banner("/admin/"), banner("/dst/admin/")
    assert "+1 (732) 555-0199" in us and "https://wa.me/17325550199" in us
    assert "us-channel-1" in us and "93600" not in us
    assert "+91 93600 56005" in dst and "https://wa.me/919360056005" in dst
    assert "6af30a65a7aa4cd16b22569a" in dst and "555-0199" not in dst


def test_a_dashboard_without_a_number_says_which_variable_to_set(admin, monkeypatch):
    from app.core.config import settings_for

    monkeypatch.setattr(settings_for(tenants.DST), "whatsapp_business_number", "")
    page = admin.get("/dst/admin/").text
    assert "WhatsApp number not set" in page and "DST_WHATSAPP_BUSINESS_NUMBER" in page


# --- DST India's own Zoho CRM ---------------------------------------------------
@pytest.fixture
def own_zoho(monkeypatch):
    """Both businesses' Zoho settings as they are, put back after the test
    (saving applies values to the live settings)."""
    from cryptography.fernet import Fernet

    from app.core.config import settings_for
    from app.services import settings_store

    key = Fernet.generate_key().decode()
    for tenant in (tenants.US, tenants.DST):
        own = settings_for(tenant)
        for name in ("zoho_client_id", "zoho_client_secret", "zoho_refresh_token",
                     "zoho_data_center"):
            monkeypatch.setattr(own, name, getattr(own, name))
        monkeypatch.setattr(own, "settings_encryption_key", key)
    monkeypatch.setattr(settings_for(tenants.US), "zoho_client_id", "1000.USCLIENT")
    monkeypatch.setattr(settings_for(tenants.US), "zoho_client_secret", "us-secret")
    monkeypatch.setattr(settings_store, "_applied", settings_store.tenants.PerTenant(set))
    return settings_for(tenants.US), settings_for(tenants.DST)


def test_dst_settings_offer_its_own_zoho_with_a_domain(admin, own_zoho):
    page = admin.get("/dst/admin/settings").text
    card = page.split("data-zoho-client", 1)[1].split("</form>", 1)[0]
    assert 'name="zoho_client_id"' in card and 'name="zoho_client_secret"' in card
    assert "/dst/admin/settings/zoho/callback" in card            # to register on Zoho
    assert '<option value="in" selected>zoho.in</option>' in card  # DST India: zoho.in
    assert '<option value="com" >zoho.com</option>' in card or '<option value="com">zoho.com</option>' in card
    assert "Connect Zoho" in card
    # Shero US's card is the one button it always was: its client is in .env.
    us = admin.get("/admin/settings").text
    assert "data-zoho-client" not in us and "Connect Zoho" in us


def test_connecting_dst_zoho_saves_its_client_and_opens_zoho_in(admin, own_zoho):
    us, dst = own_zoho
    sent = admin.post("/dst/admin/settings/zoho/connect", data={
        "zoho_data_center": "in", "zoho_client_id": "1000.DSTCLIENT",
        "zoho_client_secret": "dst-secret"})
    assert sent.status_code == 303
    to = sent.headers["location"]
    assert to.startswith("https://accounts.zoho.in/oauth/v2/auth?")
    assert "client_id=1000.DSTCLIENT" in to
    assert "redirect_uri=http%3A%2F%2Ftestserver%2Fdst%2Fadmin%2Fsettings%2Fzoho%2Fcallback" in to \
        or "%2Fdst%2Fadmin%2Fsettings%2Fzoho%2Fcallback" in to
    # Saved for DST India only; Shero US keeps its own client.
    assert (dst.zoho_client_id, dst.zoho_client_secret) == ("1000.DSTCLIENT", "dst-secret")
    assert (us.zoho_client_id, us.zoho_client_secret) == ("1000.USCLIENT", "us-secret")


def test_dst_zoho_needs_its_client_first(admin, own_zoho):
    us, dst = own_zoho
    dst.zoho_client_id = dst.zoho_client_secret = ""
    sent = admin.post("/dst/admin/settings/zoho/connect", data={"zoho_data_center": "in"})
    assert sent.status_code == 303 and sent.headers["location"].endswith("/dst/admin/settings")
