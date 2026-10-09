"""The team's WhatsApp alert for every paid order (services/order_alerts.py)
and its card on the Settings page (admin/routes/order_alerts.py)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from starlette.datastructures import FormData

from app.admin.routes import order_alerts as route
from app.core.config import settings
from app.db.models import Customer, Order
from app.integrations.gallabox import templates as tpl
from app.services import order_alerts


class Sender:
    def __init__(self, fail_for: str = ""):
        self.sent: list[tuple] = []
        self.fail_for = fail_for

    async def send_template(self, to, spec, *values, button_value=None):
        if to == self.fail_for:
            raise RuntimeError("Gallabox said no")
        self.sent.append((to, spec.name, values))


@pytest.fixture
def sender(monkeypatch):
    fake = Sender()
    monkeypatch.setattr(order_alerts, "current_sender", lambda: fake)
    monkeypatch.setattr(route, "current_sender", lambda: fake)
    return fake


def _order() -> Order:
    return Order(order_number="SHO-261009-AB12", total=Decimal("27.8"),
                 slot_label="Sat 10 Oct, 7:00 PM - 8:00 PM",
                 delivery_address="6360 Lawyers Hill Road", apartment_unit="Apt 4",
                 postal_code="21075",
                 items=[{"name": "Carrot Sambar", "quantity": 2},
                        {"name": "Lemon Rice", "quantity": 1}])


CUSTOMER = Customer(whatsapp_number="14435550142", name="Asha Menon")


def test_the_alert_carries_the_whole_order():
    assert order_alerts.values(_order(), CUSTOMER, "Shero Elkridge") == (
        "SHO-261009-AB12", "Asha Menon, +14435550142", "27.80",
        "Sat 10 Oct, 7:00 PM - 8:00 PM", "Shero Elkridge",
        "6360 Lawyers Hill Road, Apt 4, 21075", "2 x Carrot Sambar, 1 x Lemon Rice")
    assert len(tpl.ORDER_ALERT.params) == 7


def test_no_value_is_blank_or_multi_line():
    """Meta refuses an empty parameter or one with a line break."""
    order = Order(order_number="SHO-1", total=Decimal("1"), items=[],
                  delivery_address="12 Oak St\nFloor 2")
    values = order_alerts.values(order, Customer(whatsapp_number="14435550142"), "")
    assert all(v and "\n" not in v for v in values)
    assert values[5] == "12 Oak St Floor 2" and values[4] == "-"


def test_a_huge_cart_is_cut_short():
    order = _order()
    order.items = [{"name": "Dish number %d" % i, "quantity": 1} for i in range(80)]
    assert len(order_alerts.values(order, CUSTOMER, "K")[6]) == order_alerts.MAX_VALUE


async def test_every_number_gets_it(sender, monkeypatch):
    monkeypatch.setattr(settings, "order_alert_numbers", "14438011011|Manager,917401268091")
    assert await order_alerts.notify_new_order(_order(), CUSTOMER, "Shero Elkridge") == 2
    assert [(to, name) for to, name, _ in sender.sent] == [
        ("14438011011", "shero_new_order_alert"), ("917401268091", "shero_new_order_alert")]


async def test_one_bad_number_does_not_stop_the_others(sender, monkeypatch):
    sender.fail_for = "14438011011"
    monkeypatch.setattr(settings, "order_alert_numbers", "14438011011,917401268091")
    assert await order_alerts.notify_new_order(_order(), CUSTOMER, "K") == 1
    assert sender.sent[0][0] == "917401268091"


async def test_no_numbers_means_no_alerts(sender):
    assert await order_alerts.notify_new_order(_order(), CUSTOMER, "K") == 0
    assert sender.sent == []


# --- the Settings card -----------------------------------------------------------------
class _Request:
    def __init__(self, items):
        self._form = FormData(items)

    async def form(self):
        return self._form

    def url_for(self, name, **_):
        return "/admin/settings"


class _Admin:
    email = "admin@shero.us"


async def test_saving_numbers(session):
    await route.save_order_alerts(
        _Request([("number", "+1 443-801-1011"), ("name", "Manager"),
                  ("number", ""), ("name", "")]),
        session=session, current_user=_Admin())
    assert order_alerts.entries() == [order_alerts.allowlist.Entry("14438011011", "Manager")]


async def test_a_short_number_is_refused(session):
    await route.save_order_alerts(_Request([("number", "8011011"), ("name", "")]),
                                  session=session, current_user=_Admin())
    assert order_alerts.entries() == []


async def test_saving_none_turns_alerts_off(session, monkeypatch):
    monkeypatch.setattr(settings, "order_alert_numbers", "14438011011")
    await route.save_order_alerts(_Request([("number", ""), ("name", "")]),
                                  session=session, current_user=_Admin())
    assert order_alerts.entries() == []


async def test_the_test_alert_is_marked_as_a_test(sender, monkeypatch):
    monkeypatch.setattr(settings, "order_alert_numbers", "14438011011")
    await route.send_test_alert(_Request([]), current_user=_Admin())
    (to, name, values), = sender.sent
    assert to == "14438011011" and "TEST" in values[0]


def test_the_card_renders_the_saved_numbers(monkeypatch):
    from app.templating import templates

    monkeypatch.setattr(settings, "order_alert_numbers", "14438011011|Manager")
    html = templates.env.get_template("admin/_order_alerts.html").render(
        url_for=lambda name, **_: f"/{name}", alert_entries=order_alerts.entries())
    assert 'value="14438011011"' in html and 'value="Manager"' in html
    assert "Send test alert" in html and "On &middot; 1 number" in html
