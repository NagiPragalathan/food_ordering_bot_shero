"""DST India as a complete, separate business beside Shero US: its own
settings, database, prices, delivery and menu, with nothing shared by
accident (app/core/tenants.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core import tenants
from app.core.config import (DstSettings, Settings, build_settings, settings, settings_for,
                             tenant_database_url, under_prefix)
from app.db.models import DeliverySlot, MenuItem, Order, OrderStage, PaymentStatus
from app.services import dispatch, kitchen, pricing

D = Decimal
# A dish in the conftest menu.
SAMBAR = "chettinad-sambar-drumstick-sambar"


# --- settings ------------------------------------------------------------------
def test_dst_india_has_its_own_terms():
    dst = settings_for(tenants.DST)
    assert (dst.business_name, dst.currency, dst.currency_symbol) == ("DST India", "INR", "₹")
    assert (dst.payment_provider, dst.delivery_provider) == ("razorpay", "own")
    assert (dst.distance_unit, dst.postal_code_label, dst.phone_country_code) == ("km", "PIN code", "91")
    assert dst.template_prefix == "dst_" and dst.order_number_prefix == "DST"
    us = settings_for(tenants.US)
    assert (us.currency, us.payment_provider, us.delivery_provider, us.template_prefix) == \
        ("USD", "stripe", "uber", "")


def test_settings_follow_the_business_being_served():
    assert settings.business_name == "Shero US"
    with tenants.use(tenants.DST):
        assert settings.business_name == "DST India"
        assert settings.currency == "INR"
    assert settings.business_name == "Shero US"


def test_shero_us_values_never_leak_into_dst(monkeypatch, tmp_path):
    """A .env with only Shero US's lines gives DST India none of its channel,
    payments or Uber - only the shared account and sign-in values."""
    env = tmp_path / ".env"
    env.write_text("GALLABOX_CHANNEL_ID=us-channel\nGALLABOX_API_KEY=account-key\n"
                   "STRIPE_SECRET_KEY=sk_live_x\nUBER_CLIENT_ID=uber\n"
                   "ADMIN_SESSION_SECRET=shared-secret\nPUBLIC_BASE_URL=https://smo.example\n"
                   "PAY_REDIRECT_BASE_URL=https://smo.example/pay\n"
                   "DATABASE_URL=sqlite+aiosqlite:////app/data/shero.db\n"
                   "DST_GALLABOX_CHANNEL_ID=dst-channel\n", encoding="utf-8")
    for name in ("GALLABOX_CHANNEL_ID", "GALLABOX_API_KEY", "STRIPE_SECRET_KEY", "UBER_CLIENT_ID",
                 "ADMIN_SESSION_SECRET", "PUBLIC_BASE_URL", "PAY_REDIRECT_BASE_URL",
                 "DATABASE_URL", "DST_GALLABOX_CHANNEL_ID", "DST_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)

    assert Settings().gallabox_channel_id == "us-channel"
    assert DstSettings().gallabox_channel_id == "dst-channel"
    assert DstSettings().stripe_secret_key == ""

    dst = build_settings(tenants.DST)
    assert dst.gallabox_channel_id == "dst-channel"
    assert dst.stripe_secret_key == "" and dst.uber_client_id == ""
    # One Gallabox account and one admin sign-in.
    assert dst.gallabox_api_key == "account-key"
    assert dst.admin_session_secret == "shared-secret"
    # Its pages, Pay Now links and database live beside Shero US's.
    assert dst.public_base_url == "https://smo.example/dst"
    assert dst.pay_redirect_base_url == "https://smo.example/dst/pay"
    assert dst.database_url == "sqlite+aiosqlite:////app/data/shero_dst.db"


@pytest.mark.parametrize("url,expected", [
    ("sqlite+aiosqlite:///./shero_local.db", "sqlite+aiosqlite:///./shero_local_dst.db"),
    ("sqlite+aiosqlite:////app/data/shero.db", "sqlite+aiosqlite:////app/data/shero_dst.db"),
    ("sqlite+aiosqlite:///:memory:", ""),
    ("postgresql://u:p@host/db", ""),        # a server database must be created and named
])
def test_a_second_business_database_beside_a_sqlite_one(url, expected):
    assert tenant_database_url(url, "dst") == expected


def test_links_move_under_the_business_prefix():
    assert under_prefix("https://smo.shero.us", "/dst") == "https://smo.shero.us/dst"
    assert under_prefix("https://smo.shero.us/pay/", "/dst") == "https://smo.shero.us/dst/pay"
    assert under_prefix("https://pay.example.com", "/dst") == "https://pay.example.com/dst"


def test_every_business_needs_only_its_own_services():
    with tenants.use(tenants.DST):
        missing = settings.missing_credentials()
    assert "RAZORPAY_KEY_ID" in missing
    assert not any(name.startswith(("STRIPE_", "UBER_", "ZOHO_")) for name in missing)


def test_a_path_under_dst_is_dst_india():
    assert tenants.for_path("/dst/admin/menu") == tenants.DST
    assert tenants.for_path("/dst") == tenants.DST
    assert tenants.for_path("/admin/menu") == tenants.US
    assert tenants.for_path("/dstx/admin") == tenants.US


def test_per_tenant_values_are_kept_apart():
    box = tenants.PerTenant(list)
    box.get().append("us")
    with tenants.use(tenants.DST):
        assert box.get() == []
        box.get().append("dst")
    assert box.get() == ["us"]


# --- own delivery ----------------------------------------------------------------
async def test_dst_charges_its_own_delivery_fee(session, menu, outlet, monkeypatch):
    dst = settings_for(tenants.DST)
    monkeypatch.setattr(dst, "own_delivery_fee", 40.0)
    monkeypatch.setattr(dst, "free_delivery_above", 500.0)
    monkeypatch.setattr(dst, "tax_percent", 5.0)
    with tenants.use(tenants.DST):
        priced = await pricing.price_cart(
            session, [{"retailer_id": SAMBAR, "quantity": 2}],
            outlet=outlet, dropoff_address={"street": "12 Anna Salai"})
        assert priced.currency == "INR"
        assert priced.delivery_fee == D("40.00")
        assert priced.delivery_quote_failed is False
        assert priced.total == priced.dish_total + D("40.00") + priced.tax

        many = await pricing.price_cart(
            session, [{"retailer_id": SAMBAR, "quantity": 50}],
            outlet=outlet, dropoff_address={"street": "12 Anna Salai"})
        assert many.dish_total >= D("500") and many.delivery_fee == D("0.00")


async def test_dst_never_asks_uber(session, menu, outlet, monkeypatch):
    from app.integrations.uber import direct as uber

    async def refuse(*args, **kwargs):
        raise AssertionError("DST India must not ask Uber for a price")

    monkeypatch.setattr(uber.uber_direct, "get_quote", refuse)
    with tenants.use(tenants.DST):
        priced = await pricing.price_cart(
            session, [{"retailer_id": SAMBAR, "quantity": 1}],
            outlet=outlet, dropoff_address={"street": "1 Main Road"})
    assert priced.delivery_quote_failed is False


def test_own_delivery_reaches_its_own_distance(monkeypatch):
    monkeypatch.setattr(settings_for(tenants.DST), "own_delivery_max_km", 25.0)
    with tenants.use(tenants.DST):
        assert kitchen.max_km() == 25.0
    # Shero US keeps Uber's 10 miles.
    assert kitchen.max_km() == pytest.approx(16.093, abs=0.01)


async def _paid_order(session, customer, outlet, number, *, stage=OrderStage.PAID_SLOT_BOOKED,
                      starts_in=timedelta(hours=3)):
    start = datetime.now(timezone.utc) + starts_in
    slot = DeliverySlot(outlet_id=outlet.id, starts_at=start, ends_at=start + timedelta(hours=1),
                        capacity=5, reserved_count=1)
    session.add(slot)
    await session.flush()
    order = Order(order_number=number, customer_id=customer.id, outlet_id=outlet.id,
                  slot_id=slot.id, items=[], dish_total=D("200"), total=D("210"),
                  currency="INR", payment_status=PaymentStatus.PAID, stage=stage)
    session.add(order)
    await session.flush()
    return order


async def test_own_staff_get_a_delivery_queue_not_uber_bookings(session, customer, outlet):
    await _paid_order(session, customer, outlet, "DST-261116-AAAAA")
    await _paid_order(session, customer, outlet, "DST-261116-BBBBB",
                      stage=OrderStage.OUT_FOR_DELIVERY)
    await _paid_order(session, customer, outlet, "DST-261116-CCCCC",
                      starts_in=timedelta(hours=-3))
    with tenants.use(tenants.DST):
        assert dispatch.due_time(datetime.now(timezone.utc)) is None
        assert await dispatch.due_orders(session) == []
        rows = await dispatch.own_queue(session)
        status = {r.order.order_number: r.status for r in rows}
        counts = dispatch.status_counts(rows)
    assert status == {"DST-261116-AAAAA": dispatch.TO_DELIVER,
                      "DST-261116-BBBBB": dispatch.ON_THE_WAY,
                      "DST-261116-CCCCC": dispatch.MISSED}
    assert counts[dispatch.TO_DELIVER] == 1 and counts[dispatch.DELIVERED] == 0


async def test_orders_are_numbered_by_business(session):
    from app.services.orders import generate_order_number

    assert generate_order_number().startswith("SHO-")
    with tenants.use(tenants.DST):
        assert generate_order_number().startswith("DST-")


# --- the menu: the ERP's item exports ----------------------------------------------
async def test_an_unpriced_dish_is_never_sold_in_dst_india(session, menu):
    """DST India requires a price (PRICE_REQUIRED); Shero US sells every
    switched-on dish, as it always has (test_us_unaffected_by_dst.py)."""
    from sqlalchemy import select

    from app.services import menu as menu_service

    dish = (await session.execute(select(MenuItem).where(
        MenuItem.retailer_id == SAMBAR))).scalar_one()
    dish.price = D("0")
    await session.flush()
    with tenants.use(tenants.DST):
        on_sale = await menu_service.get_by_retailer_ids(session, [SAMBAR])
        assert SAMBAR not in on_sale
        assert await menu_service.needs_price(session) == 1


def test_a_dst_photo_never_replaces_the_us_photo_of_the_same_dish(monkeypatch):
    from app.services import media

    saved = []
    monkeypatch.setattr(media, "_shrink", lambda data: data)
    monkeypatch.setattr(media, "save", lambda name, data, kind: saved.append(name) or name)
    media.store_bytes(b"us-photo", key=SAMBAR)
    with tenants.use(tenants.DST):
        media.store_bytes(b"dst-photo", key=SAMBAR)
    assert len(set(saved)) == 2


def test_dst_loyalty_starts_sized_in_rupees():
    from app.services import loyalty

    assert loyalty.default_policy().minimum_earn_subtotal == 10       # Shero US, dollars
    with tenants.use(tenants.DST):
        rules = loyalty.default_policy()
    assert rules.currency == "INR" and rules.minimum_earn_subtotal == 200
    assert rules.points_per_currency * rules.point_value * 100 == 1   # about 1% back
