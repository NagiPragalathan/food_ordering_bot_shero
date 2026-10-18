"""The admin Offers page: the form's rules, and the pages themselves.

The pages are rendered through the real admin router, so a template error -
the kind that only shows when someone opens the page - fails here first.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models import AdminUser, DiscountType, Placement, Promotion
from app.services import promotion_admin
from app.services.promotion_admin import PromotionFormError, parse_form


def _form(**overrides) -> dict:
    form = {"title": "Festive offer", "code": "shero20", "discount_type": "percent",
            "discount_value": "20", "min_order": "30", "max_discount": "10",
            "usage_limit": "100", "per_customer_limit": "1", "placement": "menu",
            "display_order": "2", "starts_on": "2026-10-20", "ends_on": "2026-10-31",
            "is_active": "1"}
    form.update(overrides)
    return form


# --- the form ----------------------------------------------------------------
def test_a_full_form_parses():
    data = parse_form(_form())
    assert data.code == "SHERO20"
    assert (data.discount_type, data.discount_value, data.max_discount, data.min_order) == \
        ("percent", Decimal("20.00"), Decimal("10.00"), Decimal("30.00"))
    assert (data.usage_limit, data.per_customer_limit) == (100, 1)
    assert (data.starts_on, data.ends_on) == (date(2026, 10, 20), date(2026, 10, 31))
    assert data.placement == "menu" and data.display_order == 2 and data.is_active


@pytest.mark.parametrize("overrides,message", [
    ({"title": ""}, "title"),
    ({"code": ""}, "coupon code"),
    ({"code": "A!"}, "3 to 32"),
    ({"discount_value": "120"}, "between 1 and 100"),
    ({"discount_value": "abc"}, "must be a number"),
    ({"discount_type": "flat", "discount_value": "0"}, "how many dollars"),
    ({"ends_on": "2026-10-01"}, "before the start date"),
    ({"usage_limit": "-3"}, "cannot be negative"),
    ({"placement": "everywhere"}, "where the offer is shown"),
    ({"discount_type": "bogus"}, "discount type"),
])
def test_mistakes_are_explained(overrides, message):
    with pytest.raises(PromotionFormError, match=message):
        parse_form(_form(**overrides))


def test_blank_or_zero_limits_mean_unlimited():
    data = parse_form(_form(usage_limit="", per_customer_limit="0"))
    assert data.usage_limit is None and data.per_customer_limit is None


def test_free_delivery_ignores_value_and_cap():
    data = parse_form(_form(discount_type="free_delivery", discount_value="", max_discount="5"))
    assert data.discount_value == Decimal("0") and data.max_discount is None


def test_a_flat_discount_has_no_separate_cap():
    data = parse_form(_form(discount_type="flat", discount_value="$7.50", max_discount="3"))
    assert data.discount_value == Decimal("7.50") and data.max_discount is None


async def test_codes_are_unique(session):
    await promotion_admin.save(session, None, parse_form(_form()))
    with pytest.raises(PromotionFormError, match="already uses the code SHERO20"):
        await promotion_admin.save(session, None, parse_form(_form(title="Another")))


async def test_a_copy_is_paused_with_its_own_code(session):
    source = await promotion_admin.save(session, None, parse_form(_form()))
    copy = await promotion_admin.duplicate(session, source)
    assert copy.code != source.code and copy.code.startswith("SHERO20")
    assert copy.is_active is False and copy.title == "Festive offer (copy)"
    assert copy.discount_value == source.discount_value


# --- the pages ---------------------------------------------------------------
@pytest.fixture
def admin(session, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.admin import router
    from app.admin.deps import require_admin
    from app.core.config import settings
    from app.db.session import get_session

    monkeypatch.setattr(settings, "admin_session_secret", "test-secret")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_admin] = lambda: AdminUser(
        email="ops@shero.test", name="Ops", password_hash="x", is_active=True)
    return TestClient(app, follow_redirects=False)


async def test_the_empty_page_invites_a_first_offer(admin):
    page = admin.get("/admin/promotions")
    assert page.status_code == 200
    assert "No offers yet" in page.text and "Offers" in page.text


async def test_create_list_edit_toggle_delete(session, admin, outlet):
    created = admin.post("/admin/promotions", data=_form(outlet_id=str(outlet.id)))
    assert created.status_code == 303

    promo = (await session.execute(select(Promotion))).scalar_one()
    assert promo.code == "SHERO20" and promo.outlet_id == outlet.id
    assert promo.created_by == "ops@shero.test"

    listing = admin.get("/admin/promotions")
    assert listing.status_code == 200
    assert "SHERO20" in listing.text and "20% off, up to $10" in listing.text
    assert "Shero Edison only" in listing.text

    edit = admin.get(f"/admin/promotions/{promo.id}")
    assert edit.status_code == 200
    assert 'value="SHERO20"' in edit.text and "Orders with this code" in edit.text

    admin.post(f"/admin/promotions/{promo.id}/toggle")
    await session.refresh(promo)
    assert promo.is_active is False

    admin.post(f"/admin/promotions/{promo.id}/delete")
    assert (await session.execute(select(Promotion))).scalar_one_or_none() is None


async def test_a_rejected_form_keeps_what_was_typed(admin):
    page = admin.post("/admin/promotions", data=_form(discount_value="250",
                                                      description="Big festive sale"))
    assert page.status_code == 400
    assert "between 1 and 100" in page.text
    assert 'value="Big festive sale"' in page.text


async def test_the_new_form_suggests_a_code(admin):
    page = admin.get("/admin/promotions/new")
    assert page.status_code == 200
    assert 'name="code"' in page.text and 'value="SHERO' in page.text


async def test_status_filter(session, admin):
    session.add_all([
        Promotion(title="Live", code="LIVE1", discount_type=DiscountType.FLAT,
                  discount_value=Decimal("5"), placement=Placement.CHECKOUT),
        Promotion(title="Old", code="OLD1", discount_type=DiscountType.FLAT,
                  discount_value=Decimal("5"), placement=Placement.CHECKOUT,
                  ends_on=date(2020, 1, 1)),
    ])
    await session.flush()
    page = admin.get("/admin/promotions?status=expired")
    assert "OLD1" in page.text and "LIVE1" not in page.text
