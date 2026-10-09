"""An order address no kitchen delivers to (services/out_of_area.py).

The page refuses it, naming the address; WhatsApp gets one message a day at
most, however often the customer tries; and no later step (quote, confirm)
lets an out-of-area address through to payment.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.api.routes import order_web
from app.core.config import settings
from app.integrations.gallabox import templates as tpl
from app.integrations.gallabox.sender import use_sender
from app.services import addresses, order_link, out_of_area
from app.services.kitchen import ServiceCheck

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
# Jersey City: ~38 km (24 miles) from the Edison test kitchen's 12 km radius.
FAR = {"label": "Home", "address_line1": "30 Montgomery St", "postal_code": "07302",
       "latitude": 40.7215, "longitude": -74.0466}
NEAR = {"label": "Office", "address_line1": "1 Wood Ave", "postal_code": "08830",
        "latitude": 40.5200, "longitude": -74.4100}


class Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple] = []
        self.fail = fail

    async def send_template(self, to, spec, *values, button_value=None):
        if self.fail:
            raise RuntimeError("gallabox down")
        self.sent.append((to, spec.name, values))


class FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch):
    monkeypatch.setattr(settings, "admin_session_secret", "test-secret")


@pytest.fixture
def bot():
    recorder = Recorder()
    with use_sender(recorder):
        yield recorder


# --- the WhatsApp notice -------------------------------------------------------------
async def test_the_notice_names_the_address_and_goes_once_a_day(customer, bot):
    assert await out_of_area.notify(customer, "30 Montgomery St, 07302", now=NOW)
    assert bot.sent == [(customer.whatsapp_number, "shero_out_of_area",
                         (customer.greeting_name, "30 Montgomery St, 07302"))]

    # Trying again, and with another address, within the day: no new message.
    later = NOW + timedelta(hours=5)
    assert not await out_of_area.notify(customer, "30 Montgomery St, 07302", now=later)
    assert not await out_of_area.notify(customer, "9 Grand St, 07302", now=later)
    assert len(bot.sent) == 1

    # A day later they may hear from us again.
    assert await out_of_area.notify(customer, "9 Grand St, 07302", now=NOW + timedelta(hours=25))
    assert len(bot.sent) == 2


async def test_a_failed_send_does_not_count_as_sent(customer):
    with use_sender(Recorder(fail=True)):
        assert not await out_of_area.notify(customer, "30 Montgomery St", now=NOW)
    assert customer.out_of_area_notified_at is None


def test_the_template_ends_on_words_not_the_address():
    from app.integrations.gallabox.template_admin import check_body
    check_body(tpl.OUT_OF_AREA)       # raises if Meta would refuse it
    assert tpl.OUT_OF_AREA.params == ("customer_name", "delivery_address")


def test_the_page_message_is_in_miles():
    check = ServiceCheck(False, 38.4, "38.4 km away", kitchen_name="Edison")
    text = out_of_area.page_message(check, "30 Montgomery St, 07302")
    assert text.startswith("Sorry, we do not deliver to 30 Montgomery St, 07302 yet.")
    assert "about 23.86 miles away" in text and "km" not in text


# --- the ordering page -------------------------------------------------------------
def _token(customer) -> str:
    return order_link.build_token(customer.id)


async def test_an_address_out_of_range_is_refused_and_whatsapp_told_once(
        session, customer, outlet, bot):
    far = await addresses.save(session, customer, FAR)

    for _ in range(3):                      # trying again and again
        data = await order_web.check_address(
            FakeRequest({"address_id": str(far.id)}), _token(customer), session=session)
        assert data["ok"] is False and data["serviceable"] is False
        assert "30 Montgomery St, 07302" in data["error"]

    assert [name for _, name, _ in bot.sent] == ["shero_out_of_area"]


async def test_an_address_in_range_still_goes_through(session, customer, outlet, bot):
    near = await addresses.save(session, customer, NEAR)
    data = await order_web.check_address(
        FakeRequest({"address_id": str(near.id)}), _token(customer), session=session)
    assert data["ok"] is True and data["serviceable"] is True
    assert bot.sent == []


@pytest.mark.parametrize("step", ["quote", "confirm"])
async def test_switching_to_an_out_of_range_address_cannot_reach_payment(
        session, customer, outlet, bot, monkeypatch, step):
    """A good address checked first, then a far one sent to quote / confirm."""
    near = await addresses.save(session, customer, NEAR)
    far = await addresses.save(session, customer, FAR)
    checked = await order_web.check_address(
        FakeRequest({"address_id": str(near.id)}), _token(customer), session=session)
    assert checked["ok"] is True
    monkeypatch.setattr(order_web, "_clean_cart", lambda lines: [{"retailer_id": "x"}])

    route = getattr(order_web, step)
    data = await route(FakeRequest({"address_id": str(far.id),
                                    "slot_id": checked["slots"][0]["id"]}),
                       _token(customer), session=session)

    assert data["ok"] is False and data.get("serviceable") is False
    assert "30 Montgomery St" in data["error"]


async def test_the_page_gets_what_its_panel_shows(session, customer, outlet, bot):
    far = await addresses.save(session, customer, FAR)
    data = await order_web.check_address(
        FakeRequest({"address_id": str(far.id)}), _token(customer), session=session)

    info = data["out_of_area"]
    assert info["address"] == "30 Montgomery St, 07302"
    assert info["miles"] > 7.46                     # beyond the 12 km (7.46 mi) radius
    assert info["range_miles"] == 7.46
    assert info["notified"] is True


async def test_no_kitchen_at_all_is_not_blamed_on_the_area(session, customer, bot):
    """Nothing set up in the admin is our problem: no "not in your area" message."""
    far = await addresses.save(session, customer, FAR)
    data = await order_web.check_address(
        FakeRequest({"address_id": str(far.id)}), _token(customer), session=session)

    assert data["ok"] is False and "out_of_area" not in data
    assert bot.sent == []


async def test_the_page_gets_a_week_of_slots_all_24_hours_away(session, customer, outlet, bot):
    """The date picker: slots grouped by day, none sooner than SLOT_MIN_LEAD_HOURS."""
    from app.core.config import settings
    from app.services import slots as slots_service

    near = await addresses.save(session, customer, NEAR)
    data = await order_web.check_address(
        FakeRequest({"address_id": str(near.id)}), _token(customer), session=session)

    assert data["ok"] is True and data["lead_hours"] == settings.slot_min_lead_hours
    assert data["earliest"]                          # "Thu 08 Oct, 1:00 PM"
    days = {s["date"] for s in data["slots"]}
    assert len(days) >= 5                            # a week to choose from, not one day
    first = await slots_service.get_slot(session, data["slots"][0]["id"])
    earliest = slots_service.earliest_bookable()
    assert slots_service.as_utc(first.starts_at) >= earliest - timedelta(minutes=1)
    one = data["slots"][0]
    assert set(one) >= {"id", "date", "weekday", "day", "month", "time"}
    assert " - " in one["time"] and "," not in one["time"]


async def test_a_slot_that_came_too_close_while_the_page_was_open_is_refused(
        session, customer, outlet, bot, monkeypatch):
    """Shown at 4:59, confirmed at 5:20: the 5 PM slot is now under 24 hours away."""
    from app.services import slots as slots_service

    near = await addresses.save(session, customer, NEAR)
    data = await order_web.check_address(
        FakeRequest({"address_id": str(near.id)}), _token(customer), session=session)
    first = data["slots"][0]["id"]
    monkeypatch.setattr(order_web, "_clean_cart", lambda lines: [{"retailer_id": "x"}])

    # Time has moved on: that slot now starts sooner than the notice allows.
    slot = await slots_service.get_slot(session, first)
    later = slots_service.as_utc(slot.starts_at) + timedelta(minutes=1)
    monkeypatch.setattr(slots_service, "earliest_bookable", lambda now=None: later)
    result = await order_web.quote(FakeRequest({"address_id": str(near.id), "slot_id": first}),
                                   _token(customer), session=session)

    assert result["ok"] is False and "24 hours" in result["error"]
