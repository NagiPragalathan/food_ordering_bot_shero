"""Coupon codes: the rules, the money, and where the discount ends up.

A discount has to agree everywhere the order is shown: the summary on the
page, the order row, Stripe, the WhatsApp messages, the PDF bill and Zoho.
These tests follow one code through each of them.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core import config
from app.core.config import settings
from app.db.models import (DeliverySlot, DiscountType, Order, OrderStage, PaymentStatus,
                           Placement, Promotion)
from app.integrations.stripe_gw import checkout
from app.integrations.uber.direct import DeliveryQuote
from app.integrations.zoho import crm
from app.integrations.zoho import fields as f
from app.services import (addresses, order_changes, orders, payments, pricing, promotions,
                          receipts)
from app.services import cart as cart_service

SAMBAR = "chettinad-sambar-drumstick-sambar"   # 12.50
RASAM = "chettinad-rasam-tomato-rasam"         # 9.50


def _promo(**overrides) -> Promotion:
    values = dict(title="Festive offer", code="SHERO20", discount_type=DiscountType.PERCENT,
                  discount_value=Decimal("20"), min_order=Decimal("0"), is_active=True,
                  placement=Placement.CHECKOUT, display_order=0, first_order_only=False)
    values.update(overrides)
    return Promotion(**values)


async def _saved(session, **overrides) -> Promotion:
    promo = _promo(**overrides)
    session.add(promo)
    await session.flush()
    return promo


def _order(customer, promo, *, status=PaymentStatus.PAID, stage=OrderStage.PAID_SLOT_BOOKED,
           number="SHO-T-1", link_open=True) -> Order:
    return Order(order_number=number, customer_id=customer.id, promotion_id=promo.id,
                 promo_code=promo.code, discount=Decimal("5.00"), total=Decimal("20.00"),
                 payment_status=status, stage=stage, items=[],
                 payment_link_expires_at=datetime.now(timezone.utc)
                 + timedelta(minutes=20 if link_open else -5))


# --- the money -------------------------------------------------------------
class TestDiscountFor:
    def test_a_percentage_comes_off_the_dishes(self):
        assert promotions.discount_for(_promo(), dish_total=Decimal("40"),
                                       delivery_fee=Decimal("7.99")) == Decimal("8.00")

    def test_a_percentage_stops_at_its_cap(self):
        promo = _promo(max_discount=Decimal("5"))
        assert promotions.discount_for(promo, dish_total=Decimal("40"),
                                       delivery_fee=Decimal("0")) == Decimal("5.00")

    def test_a_percentage_rounds_half_up_to_the_cent(self):
        promo = _promo(discount_value=Decimal("15"))
        # 15% of 12.10 = 1.815
        assert promotions.discount_for(promo, dish_total=Decimal("12.10"),
                                       delivery_fee=Decimal("0")) == Decimal("1.82")

    def test_a_flat_amount_never_exceeds_the_dishes(self):
        promo = _promo(discount_type=DiscountType.FLAT, discount_value=Decimal("25"))
        assert promotions.discount_for(promo, dish_total=Decimal("12.50"),
                                       delivery_fee=Decimal("7.99")) == Decimal("12.50")

    def test_free_delivery_is_the_delivery_charge(self):
        promo = _promo(discount_type=DiscountType.FREE_DELIVERY, discount_value=Decimal("0"))
        assert promotions.discount_for(promo, dish_total=Decimal("40"),
                                       delivery_fee=Decimal("7.99")) == Decimal("7.99")


class TestAmountProblem:
    def test_below_the_minimum_says_how_much_more(self):
        promo = _promo(min_order=Decimal("30"))
        problem = promotions.amount_problem(promo, dish_total=Decimal("25.50"))
        assert problem == "Add $4.50 more to use SHERO20 (minimum order $30.00)."

    def test_at_the_minimum_it_applies(self):
        promo = _promo(min_order=Decimal("30"))
        assert promotions.amount_problem(promo, dish_total=Decimal("30.00")) is None

    def test_free_delivery_with_nothing_to_waive(self):
        promo = _promo(discount_type=DiscountType.FREE_DELIVERY)
        assert promotions.amount_problem(promo, dish_total=Decimal("30"),
                                         delivery_fee=Decimal("0")) == \
            "Delivery is already free on this order."


async def test_pricing_takes_the_discount_and_taxes_what_is_left(session, menu, monkeypatch):
    monkeypatch.setattr(config.settings, "tax_percent", 10.0)
    priced = await pricing.price_cart(session, [{"retailer_id": SAMBAR, "quantity": 2}],
                                      promotion=_promo())
    assert priced.dish_total == Decimal("25.00")
    assert priced.discount == Decimal("5.00")
    assert priced.tax == Decimal("2.00")               # 10% of 20.00, not of 25.00
    assert priced.total == Decimal("22.00")
    assert priced.total_without_coupon == Decimal("27.50")   # 25.00 + 10% tax on 25.00
    assert priced.promo_code == "SHERO20"


async def test_free_delivery_does_not_change_the_tax(session, menu, monkeypatch, outlet):
    monkeypatch.setattr(config.settings, "tax_percent", 10.0)

    async def quote(**_):
        return DeliveryQuote(quote_id="q-1", fee=Decimal("7.99"))
    monkeypatch.setattr(pricing, "_quote_delivery", quote)

    promo = _promo(discount_type=DiscountType.FREE_DELIVERY, discount_value=Decimal("0"))
    priced = await pricing.price_cart(session, [{"retailer_id": SAMBAR, "quantity": 2}],
                                      outlet=outlet, dropoff_address={"street": "x"},
                                      promotion=promo)
    assert priced.discount == Decimal("7.99")
    assert priced.tax == Decimal("2.50")
    assert priced.total == Decimal("27.50")             # 25 + 7.99 + 2.50 - 7.99


async def test_a_cart_below_the_minimum_keeps_the_code_without_the_discount(session, menu):
    priced = await pricing.price_cart(session, [{"retailer_id": RASAM, "quantity": 1}],
                                      promotion=_promo(min_order=Decimal("20")))
    assert priced.discount == Decimal("0.00")
    assert priced.promo_code is None
    assert "Add $10.50 more" in priced.promo_problem
    assert priced.total == Decimal("9.50")


async def test_the_total_never_drops_below_stripes_minimum(session, menu):
    promo = _promo(discount_type=DiscountType.PERCENT, discount_value=Decimal("100"))
    priced = await pricing.price_cart(session, [{"retailer_id": RASAM, "quantity": 1}],
                                      promotion=promo)
    assert priced.total == promotions.MIN_CHARGE
    assert priced.discount == Decimal("9.00")


def test_the_chat_summary_shows_the_coupon_line():
    priced = pricing.PricedOrder(dish_total=Decimal("25.00"), delivery_fee=Decimal("7.99"),
                                 discount=Decimal("5.00"), total=Decimal("27.99"),
                                 promotion=_promo())
    text = pricing.format_summary(priced, outlet_name="Shero Edison", slot_label="Tue 11:00")
    assert "Coupon SHERO20: -$5.00" in text
    assert "*Total: $27.99*" in text
    assert "You save $5.00" in text


# --- the rules -------------------------------------------------------------
async def test_an_unknown_code_is_refused(session, customer, outlet):
    with pytest.raises(promotions.PromoError, match="not a valid code"):
        await promotions.resolve(session, "nope", customer=customer, outlet=outlet)


async def test_codes_are_matched_whatever_the_case_and_spacing(session, customer, outlet):
    await _saved(session)
    found = await promotions.resolve(session, " shero 20 ", customer=customer, outlet=outlet)
    assert found.code == "SHERO20"


@pytest.mark.parametrize("overrides,message", [
    ({"is_active": False}, "not active"),
    ({"ends_on": date(2020, 1, 1)}, "has expired"),
    ({"starts_on": date(2099, 3, 4)}, "starts on Mar 4"),
])
async def test_a_code_outside_its_window_is_refused(session, customer, outlet,
                                                     overrides, message):
    await _saved(session, **overrides)
    with pytest.raises(promotions.PromoError, match=message):
        await promotions.resolve(session, "SHERO20", customer=customer, outlet=outlet)


async def test_the_end_date_is_included(session, customer, outlet):
    today = await promotions.local_today(session, outlet)
    await _saved(session, starts_on=today, ends_on=today)
    assert await promotions.resolve(session, "SHERO20", customer=customer, outlet=outlet)


async def test_a_code_for_another_kitchen_is_refused(session, customer, outlet, far_outlet):
    await _saved(session, outlet_id=far_outlet.id)
    with pytest.raises(promotions.PromoError, match="kitchen"):
        await promotions.resolve(session, "SHERO20", customer=customer, outlet=outlet)


async def test_the_overall_limit_counts_paid_and_open_unpaid_orders(session, customer, outlet):
    from app.db.models import Customer
    other = Customer(whatsapp_number="17325550199", name="Ravi")
    session.add(other)
    await session.flush()
    promo = await _saved(session, usage_limit=2)
    session.add(_order(other, promo, number="SHO-T-1"))
    session.add(_order(other, promo, number="SHO-T-2", status=PaymentStatus.LINK_SENT,
                       stage=OrderStage.PENDING_PAYMENT))
    await session.flush()

    with pytest.raises(promotions.PromoError, match="fully claimed"):
        await promotions.resolve(session, "SHERO20", customer=customer, outlet=outlet)


async def test_an_expired_link_gives_its_use_back(session, customer, outlet):
    from app.db.models import Customer
    other = Customer(whatsapp_number="17325550199", name="Ravi")
    session.add(other)
    await session.flush()
    promo = await _saved(session, usage_limit=1)
    session.add(_order(other, promo, status=PaymentStatus.LINK_SENT,
                       stage=OrderStage.PENDING_PAYMENT, link_open=False))
    await session.flush()

    assert await promotions.resolve(session, "SHERO20", customer=customer, outlet=outlet)


async def test_once_per_customer(session, customer, outlet):
    promo = await _saved(session, per_customer_limit=1)
    session.add(_order(customer, promo))
    await session.flush()
    with pytest.raises(promotions.PromoError, match="already used"):
        await promotions.resolve(session, "SHERO20", customer=customer, outlet=outlet)


async def test_a_refunded_order_does_not_use_up_the_code(session, customer, outlet):
    promo = await _saved(session, per_customer_limit=1)
    session.add(_order(customer, promo, status=PaymentStatus.REFUNDED,
                       stage=OrderStage.REFUNDED))
    await session.flush()
    assert await promotions.resolve(session, "SHERO20", customer=customer, outlet=outlet)


async def test_first_order_only(session, customer, outlet):
    first = await _saved(session, first_order_only=True)
    assert await promotions.resolve(session, "SHERO20", customer=customer, outlet=outlet)

    other = await _saved(session, code="OTHER5")
    session.add(_order(customer, other))
    await session.flush()
    with pytest.raises(promotions.PromoError, match="first order only"):
        await promotions.resolve(session, first.code, customer=customer, outlet=outlet)


async def test_usage_is_counted_per_promotion(session, customer):
    promo = await _saved(session)
    session.add(_order(customer, promo, number="SHO-T-1"))
    session.add(_order(customer, promo, number="SHO-T-2", status=PaymentStatus.LINK_SENT,
                       stage=OrderStage.PENDING_PAYMENT))
    await session.flush()

    usage = (await promotions.usage(session, [promo.id]))[promo.id]
    assert (usage.paid, usage.held, usage.used) == (1, 1, 2)
    assert usage.discount_given == Decimal("5.00") and usage.revenue == Decimal("20.00")


async def test_the_usage_query_works_on_mysql():
    """The AWS server runs MySQL, which has no aggregate FILTER clause."""
    import uuid

    from sqlalchemy.dialects import mysql

    captured = []

    class Capture:
        async def execute(self, statement):
            captured.append(statement)
            return []

    await promotions.usage(Capture(), [uuid.uuid4()])
    sql = str(captured[0].compile(dialect=mysql.dialect())).upper()
    assert "FILTER" not in sql and "CASE WHEN" in sql


def test_status_for_the_admin_list():
    today = date(2026, 10, 18)
    assert promotions.status(_promo(), today=today) == promotions.LIVE
    assert promotions.status(_promo(is_active=False), today=today) == promotions.PAUSED
    assert promotions.status(_promo(ends_on=date(2026, 10, 17)), today=today) == promotions.EXPIRED
    assert promotions.status(_promo(starts_on=date(2026, 10, 19)), today=today) == promotions.SCHEDULED
    assert promotions.status(_promo(usage_limit=3), today=today, used=3) == promotions.USED_UP


async def test_listed_offers_leave_out_hidden_and_unusable_codes(session, customer, outlet):
    await _saved(session, code="MENU10", placement=Placement.MENU, display_order=2)
    await _saved(session, code="BIG30", min_order=Decimal("30"), display_order=1)
    await _saved(session, code="SECRET", placement=Placement.HIDDEN)
    await _saved(session, code="OLD", ends_on=date(2020, 1, 1))

    offers = await promotions.offers_for(session, customer, outlet=outlet,
                                         dish_total=Decimal("25"))
    assert [o["code"] for o in offers] == ["BIG30", "MENU10"]
    big = offers[0]
    assert big["eligible"] is False and big["hint"].startswith("Add $5.00 more")
    assert offers[1]["eligible"] is True and offers[1]["saving"] == 5.0


def test_describing_an_offer():
    promo = _promo(max_discount=Decimal("10"), min_order=Decimal("30"),
                   per_customer_limit=1, ends_on=date(2026, 9, 9))
    assert promotions.headline(promo) == "20% off, up to $10"
    assert promotions.default_tag(promo) == "FLAT 20% OFF"
    assert promotions.conditions(promo) == ["Min order $30", "Once per customer",
                                            "Valid till Sep 9"]
    flat = _promo(discount_type=DiscountType.FLAT, discount_value=Decimal("7.50"))
    assert promotions.headline(flat) == "$7.50 off"


# --- where the discount ends up --------------------------------------------
async def test_the_order_keeps_a_copy_of_the_coupon(session, customer, outlet, menu):
    promo = await _saved(session)
    priced = await pricing.price_cart(session, [{"retailer_id": SAMBAR, "quantity": 2}],
                                      promotion=promo)
    order = await orders.create_draft_order(session, customer=customer, outlet=outlet,
                                            priced=priced)
    assert (order.discount, order.promo_code, order.promo_title, order.promotion_id) == \
        (Decimal("5.00"), "SHERO20", "Festive offer", promo.id)
    assert order.total == Decimal("20.00")


async def test_stripe_gets_a_one_off_coupon_for_the_exact_discount(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_123")
    made: dict = {}

    class Coupon:
        @staticmethod
        async def create_async(**params):
            made["coupon"] = params
            return type("C", (), {"id": "co_1"})

    class Session:
        @staticmethod
        async def create_async(**params):
            made["session"] = params
            return type("S", (), {"id": "cs_1", "url": "https://stripe/cs_1"})

    monkeypatch.setattr(checkout.stripe, "Coupon", Coupon)
    monkeypatch.setattr(checkout.stripe.checkout, "Session", Session)

    await checkout.create_checkout_session(
        order_id="o-1", order_number="SHO-T-1",
        items=[{"name": "Sambar", "quantity": 2, "unit_price": "12.50"}],
        delivery_fee=Decimal("7.99"), taxes_and_fees=Decimal("0"), currency="usd",
        discount=Decimal("5.00"), discount_label="Coupon SHERO20")

    assert made["coupon"]["amount_off"] == 500
    assert made["coupon"]["max_redemptions"] == 1 and made["coupon"]["name"] == "Coupon SHERO20"
    assert made["session"]["discounts"] == [{"coupon": "co_1"}]


async def test_a_key_that_cannot_make_coupons_still_charges_the_discounted_total(monkeypatch):
    # The live restricted key lacked "Coupons: write"; every coupon order then
    # failed with "We could not start the payment".
    monkeypatch.setattr(settings, "stripe_secret_key", "rk_live_123")
    made: dict = {}

    class Coupon:
        @staticmethod
        async def create_async(**params):
            raise checkout.stripe.PermissionError(
                "Enabling Coupons Write ('coupon_write') permissions on this key...")

    class Session:
        @staticmethod
        async def create_async(**params):
            made["session"] = params
            return type("S", (), {"id": "cs_1", "url": "https://stripe/cs_1"})

    monkeypatch.setattr(checkout.stripe, "Coupon", Coupon)
    monkeypatch.setattr(checkout.stripe.checkout, "Session", Session)

    result = await checkout.create_checkout_session(
        order_id="o-1", order_number="SHO-261020-0SP8H",
        items=[{"name": "Murungaikai ( Drumstick ) Sambar", "quantity": 1, "unit_price": "16.53"},
               {"name": "Rasam", "quantity": 2, "unit_price": "4.00"}],
        delivery_fee=Decimal("7.99"), taxes_and_fees=Decimal("0.69"), currency="usd",
        discount=Decimal("4.96"), discount_label="Coupon SHERO30")

    assert result["url"] == "https://stripe/cs_1"
    assert "discounts" not in made["session"]
    [line] = made["session"]["line_items"]
    assert line["quantity"] == 1
    assert line["price_data"]["unit_amount"] == 1653 + 800 + 799 + 69 - 496
    assert line["price_data"]["product_data"]["name"] == "Order SHO-261020-0SP8H"
    assert line["price_data"]["product_data"]["description"] == (
        "Murungaikai ( Drumstick ) Sambar $16.53 · 2 × Rasam $8.00 · Delivery charge $7.99"
        " · Taxes and fees $0.69 · Coupon SHERO30 -$4.96")


async def test_no_discount_means_no_stripe_coupon(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_123")
    made: dict = {}

    class Session:
        @staticmethod
        async def create_async(**params):
            made["session"] = params
            return type("S", (), {"id": "cs_1", "url": "https://stripe/cs_1"})

    monkeypatch.setattr(checkout.stripe.checkout, "Session", Session)
    await checkout.create_checkout_session(
        order_id="o-1", order_number="SHO-T-1",
        items=[{"name": "Sambar", "quantity": 1, "unit_price": "12.50"}],
        delivery_fee=Decimal("0"), taxes_and_fees=Decimal("0"))
    assert "discounts" not in made["session"]


def test_whatsapp_amounts_mention_the_saving():
    order = Order(total=Decimal("20.00"), discount=Decimal("5.00"), promo_code="SHERO20")
    assert payments.amount_text(order) == "20.00 (you saved $5.00 with SHERO20)"
    assert payments.amount_text(Order(total=Decimal("20.00"))) == "20.00"


def test_the_bill_draws_with_a_coupon_line(customer, outlet):
    order = Order(order_number="SHO-T-1", customer_id=customer.id,
                  items=[{"retailer_id": SAMBAR, "name": "Drumstick Sambar",
                          "quantity": 2, "unit_price": "12.50"}],
                  dish_total=Decimal("25.00"), delivery_fee=Decimal("7.99"),
                  extra_fees=Decimal("0"), tax=Decimal("0"), discount=Decimal("5.00"),
                  promo_code="SHERO20", promo_title="Festive offer", total=Decimal("27.99"),
                  currency="USD", paid_at=datetime.now(timezone.utc))
    pdf = receipts.build_pdf(order, customer, outlet)
    assert pdf.startswith(b"%PDF")


def test_zoho_order_records_the_discount():
    order = Order(order_number="SHO-T-1", stage=OrderStage.PAID_SLOT_BOOKED,
                  items=[{"retailer_id": SAMBAR, "name": "Sambar", "quantity": 2,
                          "unit_price": "12.50"}],
                  dish_total=Decimal("25.00"), delivery_fee=Decimal("7.99"),
                  extra_fees=Decimal("0"), tax=Decimal("0"), discount=Decimal("5.00"),
                  promo_code="SHERO20", promo_title="Festive offer", total=Decimal("27.99"))
    fields = crm.order_fields(order, zoho_contact_id="c-1", zoho_lead_id=None,
                              outlet_name="Shero", cuisine=None)
    assert fields[f.O_DISCOUNT] == Decimal("5.00") and fields[f.O_COUPON_CODE] == "SHERO20"
    assert fields[f.O_ITEMS].splitlines()[-1] == "Coupon SHERO20 (Festive offer) = -5.00"

    so = crm.sales_order_fields(order, product_ids={SAMBAR: "p-1"}, zoho_contact_id="c-1")
    # Grand Total = grid 25.00 + Adjustment = 27.99, what was paid.
    assert so[f.SO_ADJUSTMENT] == pytest.approx(2.99)
    assert "less the coupon" in so[f.SO_DESCRIPTION]


async def test_a_crm_without_the_coupon_fields_still_gets_the_order(monkeypatch):
    """Before setup_zoho_crm.py adds Discount and Coupon Code, Zoho may refuse
    them; the Order is then filed without them rather than not at all."""
    from app.core.exceptions import IntegrationError
    sent = []

    async def create_record(module, payload):
        sent.append(dict(payload))
        for name in (f.O_DISCOUNT, f.O_COUPON_CODE):
            if name in payload:
                raise IntegrationError("zoho", "record write failed", payload={
                    "code": "INVALID_DATA", "details": {"api_name": name}})
        return "zorder-1"
    monkeypatch.setattr(crm.zoho_client, "create_record", create_record)

    order_id = await crm.create_order({"Name": "SHO-T-1", f.O_ORDER_TOTAL: Decimal("20"),
                                       f.O_DISCOUNT: Decimal("5"), f.O_COUPON_CODE: "SAVE5"})
    assert order_id == "zorder-1" and len(sent) == 3
    assert f.O_DISCOUNT not in sent[-1] and f.O_COUPON_CODE not in sent[-1]
    assert sent[-1][f.O_ORDER_TOTAL] == 20.0


async def test_other_zoho_refusals_are_not_hidden(monkeypatch):
    from app.core.exceptions import IntegrationError

    async def create_record(module, payload):
        raise IntegrationError("zoho", "record write failed", payload={
            "code": "INVALID_DATA", "details": {"api_name": f.O_ORDER_TOTAL}})
    monkeypatch.setattr(crm.zoho_client, "create_record", create_record)
    with pytest.raises(IntegrationError):
        await crm.create_order({"Name": "SHO-T-1", f.O_ORDER_TOTAL: Decimal("20")})


def test_zoho_is_not_sent_coupon_fields_without_a_coupon():
    order = Order(order_number="SHO-T-1", stage=OrderStage.PAID_SLOT_BOOKED, items=[],
                  total=Decimal("10"), discount=Decimal("0"))
    fields = f.compact(crm.order_fields(order, zoho_contact_id="c-1", zoho_lead_id=None,
                                        outlet_name=None, cuisine=None))
    assert f.O_DISCOUNT not in fields and f.O_COUPON_CODE not in fields


async def test_a_new_order_with_the_code_releases_the_older_unpaid_one(session, customer):
    promo = await _saved(session)
    old = _order(customer, promo, number="SHO-OLD", status=PaymentStatus.LINK_SENT,
                 stage=OrderStage.PENDING_PAYMENT)
    new = _order(customer, promo, number="SHO-NEW", status=PaymentStatus.LINK_SENT,
                 stage=OrderStage.PENDING_PAYMENT)
    session.add_all([old, new])
    await session.flush()

    assert await order_changes.release_other_promo_holds(session, new) == ["SHO-OLD"]
    assert old.stage == OrderStage.CANCELLED and old.payment_status == PaymentStatus.EXPIRED
    assert new.stage == OrderStage.PENDING_PAYMENT


async def test_paying_forgets_the_applied_code(session, customer, outlet, monkeypatch):
    from app.services import crm_sync

    async def nothing(*_, **__):
        return True
    monkeypatch.setattr(payments, "send_payment_confirmation", nothing)
    monkeypatch.setattr(crm_sync, "convert_and_record_order", nothing)

    promo = await _saved(session)
    conversation, _ = await cart_service.for_customer(session, customer)
    promotions.remember(conversation, "SHERO20")
    order = _order(customer, promo, status=PaymentStatus.LINK_SENT,
                   stage=OrderStage.PENDING_PAYMENT)
    order.outlet_id = outlet.id
    session.add(order)
    await session.flush()

    assert await payments.handle_payment_success(session, order, customer, "pi_1")
    assert promotions.stored_code(conversation) is None


# --- the ordering page, end to end -----------------------------------------
class FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class Sender:
    def __init__(self):
        self.sent = []

    async def send_template(self, to, spec, *values, button_value=None):
        self.sent.append((spec.name, values))


@pytest.fixture
async def web(session, customer, outlet, menu, monkeypatch):
    """A customer with 2 x Drumstick Sambar ($25) in the cart, an address,
    a slot, and Uber quoting $7.99."""
    from app.api.routes import order_web
    from app.services import order_link

    monkeypatch.setattr(settings, "admin_session_secret", "test-secret")
    monkeypatch.setattr(settings, "tax_percent", 0.0)

    async def quote(**_):
        return DeliveryQuote(quote_id="q-1", fee=Decimal("7.99"))
    monkeypatch.setattr(pricing, "_quote_delivery", quote)
    sender = Sender()
    monkeypatch.setattr(payments, "current_sender", lambda: sender)

    conversation, _ = await cart_service.for_customer(session, customer)
    cart_service.set_quantity(conversation, retailer_id=SAMBAR, name="Drumstick Sambar",
                              price="12.50", quantity=2)
    address = await addresses.save(session, customer, {
        "label": "Home", "address_line1": "12 Maple Street", "postal_code": "08820",
        "latitude": customer.latitude, "longitude": customer.longitude})
    start = datetime.now(timezone.utc) + timedelta(days=2)
    slot = DeliverySlot(outlet_id=outlet.id, starts_at=start,
                        ends_at=start + timedelta(hours=1), capacity=5)
    session.add(slot)
    customer.preferred_outlet_id = outlet.id
    await session.flush()

    class Web:
        token = order_link.build_token(customer.id)
        body = {"slot_id": str(slot.id), "address_id": str(address.id)}

        async def call(self, name, payload=None):
            handler = getattr(order_web, name)
            if payload is None:
                return await handler(self.token, session=session)
            return await handler(FakeRequest(payload), self.token, session=session)
    Web.sender = sender
    return Web()


async def test_apply_quote_and_confirm_with_a_coupon(session, web):
    await _saved(session, code="SAVE5", discount_type=DiscountType.FLAT,
                 discount_value=Decimal("5"), placement=Placement.MENU)

    listed = await web.call("offers")
    assert [o["code"] for o in listed["offers"]] == ["SAVE5"] and listed["applied"] is None

    applied = await web.call("apply_coupon", {"code": "save5"})
    assert applied["ok"] and applied["coupon"]["code"] == "SAVE5"

    quoted = await web.call("quote", web.body)
    assert quoted["ok"]
    assert quoted["discount"] == 5.0 and quoted["total"] == pytest.approx(27.99)
    assert quoted["total_before_discount"] == pytest.approx(32.99)
    assert quoted["coupon"]["applied"] is True and quoted["coupon"]["saving"] == 5.0

    placed = await web.call("confirm", {**web.body, "coupon_code": "SAVE5"})
    assert placed["ok"], placed
    assert placed["discount"] == 5.0 and placed["coupon"] == "SAVE5"

    order = await orders.get_by_number(session, placed["order_number"])
    assert order.total == Decimal("27.99") and order.promo_code == "SAVE5"
    # The WhatsApp summary carries the discounted total and the saving.
    (name, values), = web.sender.sent
    assert values[-1] == "27.99 (you saved $5.00 with SAVE5)"


async def test_a_bad_code_is_explained_and_not_kept(session, web):
    result = await web.call("apply_coupon", {"code": "NOPE"})
    assert result == {"ok": False, "error": "NOPE is not a valid code. Check it and try again."}
    quoted = await web.call("quote", web.body)
    assert quoted["coupon"] is None and quoted["discount"] == 0


async def test_a_coupon_that_stopped_working_asks_for_a_second_look(session, web):
    promo = await _saved(session, code="SAVE5", discount_type=DiscountType.FLAT,
                         discount_value=Decimal("5"))
    await web.call("apply_coupon", {"code": "SAVE5"})
    promo.is_active = False               # paused by the admin after the quote
    await session.flush()

    result = await web.call("confirm", {**web.body, "coupon_code": "SAVE5"})
    assert result["ok"] is False and result["requote"] is True
    assert "SAVE5 is not active right now" in result["error"]
    assert await session.scalar(Order.__table__.select().with_only_columns(
        Order.id).limit(1)) is None       # nothing was created


async def test_removing_the_coupon(session, web):
    await _saved(session, code="SAVE5", discount_type=DiscountType.FLAT,
                 discount_value=Decimal("5"))
    await web.call("apply_coupon", {"code": "SAVE5"})
    assert (await web.call("remove_coupon"))["ok"]
    quoted = await web.call("quote", web.body)
    assert quoted["coupon"] is None and quoted["total"] == pytest.approx(32.99)
