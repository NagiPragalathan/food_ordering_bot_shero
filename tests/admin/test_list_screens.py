"""Search, filters and pages on the admin's list screens: Customers, the Uber
queue and Kitchens (app/admin/listing.py and the services behind them)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from starlette.requests import Request

from app.admin import listing
from app.db.models import Customer, Order
from app.services import customer_admin, dispatch, kitchen_admin

NOW = datetime(2026, 10, 7, 16, 0, tzinfo=timezone.utc)      # Wed 12:00 in New York


def _request(query: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/admin/customers",
                    "query_string": query.encode(), "headers": []})


# --- URLs and pages ------------------------------------------------------------------
def test_a_new_filter_keeps_the_others_and_starts_on_page_one():
    request = _request("q=asha&stage=Converted&page=3")
    assert listing.url_with(request, sort="name") == \
        "/admin/customers?q=asha&stage=Converted&sort=name"
    assert listing.url_with(request, page=4) == "/admin/customers?q=asha&stage=Converted&page=4"
    assert listing.url_with(request, stage=None) == "/admin/customers?q=asha"


def test_pages_clamp_to_what_exists():
    page = listing.Page.of("9", 25, 60)
    assert (page.number, page.pages, page.first, page.last) == (3, 3, 51, 60)
    assert listing.Page.of("rubbish", 25, 0).number == 1
    assert not listing.Page.of(1, 25, 60).has_prev and listing.Page.of(1, 25, 60).has_next


def test_an_unknown_filter_value_falls_back():
    assert listing.choice("paid", customer_admin.ACTIVITY, "") == "paid"
    assert listing.choice("<script>", customer_admin.ACTIVITY, "") == ""


def test_time_ago_reads_naturally():
    assert listing.ago(NOW - timedelta(seconds=20), NOW) == "just now"
    assert listing.ago(NOW - timedelta(minutes=5), NOW) == "5 min ago"
    assert listing.ago(NOW - timedelta(days=2), NOW) == "2 days ago"
    assert listing.ago(None, NOW) == "never"


# --- Customers -------------------------------------------------------------------------
async def _people(session, outlet):
    paid = Customer(whatsapp_number="14155550101", name="Asha Menon",
                    lead_stage="Converted", zoho_contact_id="z1")
    browsing = Customer(whatsapp_number="14155550102", name="Ben Ortiz", lead_stage="Cart Created")
    quiet = Customer(whatsapp_number="14155550103", lead_stage="New Enquiry")
    session.add_all([paid, browsing, quiet])
    await session.flush()
    session.add(Order(order_number="SHO-1", customer_id=paid.id, outlet_id=outlet.id,
                      items=[], dish_total=10, delivery_fee=0, extra_fees=0, tax=0, total=10,
                      payment_status="paid"))
    await session.flush()
    return paid, browsing, quiet


async def test_customers_filter_by_orders_stage_zoho_and_search(session, outlet):
    paid, browsing, quiet = await _people(session, outlet)
    names = lambda rows: {r.customer.whatsapp_number for r in rows}  # noqa: E731

    assert names(await customer_admin.list_customers(session, activity="paid")) == {paid.whatsapp_number}
    assert quiet.whatsapp_number in names(await customer_admin.list_customers(session, activity="none"))
    assert names(await customer_admin.list_customers(session, stage="Cart Created")) == {browsing.whatsapp_number}
    assert paid.whatsapp_number not in names(await customer_admin.list_customers(session, zoho="unlinked"))
    # A number searched with its leading +.
    assert names(await customer_admin.list_customers(session, "+14155550102")) == {browsing.whatsapp_number}
    assert await customer_admin.count_customers(session, None, activity="paid") == 1


async def test_customers_sort_by_name_puts_the_nameless_last(session, outlet):
    await _people(session, outlet)
    rows = await customer_admin.list_customers(session, sort="name")
    named = [r.customer.name for r in rows if r.customer.name]
    assert named == sorted(named, key=str.lower)
    assert rows[-1].customer.name is None


async def test_customer_numbers(session, outlet):
    await _people(session, outlet)
    stats = await customer_admin.customer_stats(session, since=datetime(2000, 1, 1, tzinfo=timezone.utc))
    assert stats["paying"] == 1
    assert stats["total"] >= 3 and stats["new"] == stats["total"]
    assert stats["unlinked"] == stats["total"] - 1


# --- Uber queue -------------------------------------------------------------------------
def _row(status, start, *, name="Asha Menon", number="SHO-1", address="1 Wood Ave", zip_code="08830"):
    order = SimpleNamespace(order_number=number, delivery_address=address, postal_code=zip_code)
    customer = SimpleNamespace(name=name, whatsapp_number="14155550101")
    return dispatch.QueueRow(order=order, customer=customer, slot_start=start,
                             slot_end=start + timedelta(hours=1), send_at=None, status=status)


def test_the_queue_search_finds_order_customer_address_and_zip():
    row = _row("Waiting", NOW + timedelta(hours=3))
    for query in ("sho-1", "asha", "wood ave", "08830", "+14155550101"):
        assert dispatch.matches(row, query=query, now=NOW), query
    assert not dispatch.matches(row, query="nobody", now=NOW)


def test_the_queue_date_filter_uses_the_kitchens_day():
    new_york = ZoneInfo("America/New_York")
    tonight = _row("Waiting", datetime(2026, 10, 7, 23, 0, tzinfo=new_york))
    tomorrow = _row("Waiting", datetime(2026, 10, 8, 12, 0, tzinfo=new_york))
    yesterday = _row("Missed", datetime(2026, 10, 6, 12, 0, tzinfo=new_york))

    today = [r for r in (tonight, tomorrow, yesterday)
             if dispatch.matches(r, when="today", now=NOW, tz=new_york)]
    assert today == [tonight]
    assert not dispatch.matches(yesterday, when="upcoming", now=NOW, tz=new_york)
    assert dispatch.matches(yesterday, when="past", now=NOW, tz=new_york)


def test_the_queue_tabs_count_each_status():
    rows = [_row("Waiting", NOW), _row("Waiting", NOW), _row("Missed", NOW)]
    counts = dispatch.status_counts(rows)
    assert counts["Waiting"] == 2 and counts["Missed"] == 1 and counts["Booked"] == 0


# --- Kitchens ---------------------------------------------------------------------------
def test_open_now_is_read_in_the_kitchens_timezone(outlet):
    # The test kitchen opens 11:00-21:00 every day, in New York.
    outlet.timezone = "America/New_York"
    state = kitchen_admin.open_state(outlet, NOW)                 # 12:00 there
    assert state.open_now and state.today == ("11:00", "21:00")
    late = kitchen_admin.open_state(outlet, NOW + timedelta(hours=10))   # 22:00 there
    assert not late.open_now


def test_a_day_without_hours_is_closed(outlet):
    outlet.operating_hours = {}
    assert kitchen_admin.open_state(outlet, NOW) == kitchen_admin.OpenState(False, None)


async def test_pausing_and_resuming_a_kitchen(session, outlet):
    await kitchen_admin.set_active(session, outlet, False)
    assert outlet.is_active is False
    await kitchen_admin.set_active(session, outlet, True)
    assert outlet.is_active is True
