"""Opening hours parsing for the kitchen settings form.

These exist because the form originally had no opening-hours fields at all.
The kitchen saved cleanly, looked correct in the dashboard, and then every
customer was told "there are no delivery slots" at step 13 - `operating_hours`
stayed `{}` and slot generation produces nothing from an empty week.

So the contract worth protecting is narrow: what the form posts must come back
in exactly the shape `slots._windows_for_day` reads.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.admin.routes.settings import _parse_hours
from app.services.slots import WEEKDAY_KEYS, _windows_for_day


def test_a_full_week_parses_into_the_shape_slot_generation_reads():
    form = {}
    for key in WEEKDAY_KEYS:
        form[f"hours_{key}_open"] = "11:00"
        form[f"hours_{key}_close"] = "21:00"

    assert _parse_hours(form) == {key: [["11:00", "21:00"]] for key in WEEKDAY_KEYS}


def test_a_blank_day_is_closed_rather_than_stored_empty():
    form = {
        "hours_mon_open": "11:00", "hours_mon_close": "21:00",
        "hours_tue_open": "", "hours_tue_close": "",
        # Half-filled: an open time with no close is not a usable window.
        "hours_wed_open": "11:00", "hours_wed_close": "",
    }
    assert _parse_hours(form) == {"mon": [["11:00", "21:00"]]}


def test_a_missing_form_field_is_not_an_error():
    """The form may be posted by something that omits days entirely."""
    assert _parse_hours({}) == {}


@pytest.mark.parametrize("bad", ["25:00", "lunchtime", "11", "11:00:00:00", "-1:00"])
def test_junk_times_are_dropped_not_stored(bad):
    form = {"hours_mon_open": bad, "hours_mon_close": "21:00"}
    assert _parse_hours(form) == {}


@pytest.mark.parametrize("raw,expected", [("11:00", "11:00"), ("11:00:00", "11:00"),
                                          ("09:05", "09:05"), (" 11:00 ", "11:00")])
def test_times_are_stored_canonically(raw, expected):
    """Whatever the browser posts, the stored blob is HH:MM.

    Slot generation re-parses these strings, so the JSON must not accumulate
    several spellings of the same time.
    """
    form = {"hours_mon_open": raw, "hours_mon_close": "21:00"}
    assert _parse_hours(form) == {"mon": [[expected, "21:00"]]}


def test_parsed_hours_actually_produce_delivery_windows(outlet):
    """The real point: parse -> store -> generate has to yield slots.

    Asserting the dict shape alone would not have caught the original bug,
    because the bug was that nothing ever populated it.
    """
    outlet.operating_hours = _parse_hours({
        "hours_mon_open": "11:00", "hours_mon_close": "15:00",
        "hours_tue_open": "11:00", "hours_tue_close": "15:00",
        "hours_wed_open": "11:00", "hours_wed_close": "15:00",
        "hours_thu_open": "11:00", "hours_thu_close": "15:00",
        "hours_fri_open": "11:00", "hours_fri_close": "15:00",
        "hours_sat_open": "11:00", "hours_sat_close": "15:00",
        "hours_sun_open": "11:00", "hours_sun_close": "15:00",
    })
    outlet.slot_length_minutes = 60

    from zoneinfo import ZoneInfo
    windows = _windows_for_day(outlet, date(2026, 9, 23), ZoneInfo(outlet.timezone))

    assert len(windows) == 4          # 11-12, 12-13, 13-14, 14-15
    assert windows[0][0].hour == 11
    assert windows[-1][1].hour == 15


def test_an_empty_week_produces_no_windows(outlet):
    """The failing case, stated directly: no hours means no slots."""
    outlet.operating_hours = _parse_hours({})

    from zoneinfo import ZoneInfo
    windows = _windows_for_day(outlet, date(2026, 9, 23), ZoneInfo(outlet.timezone))

    assert windows == []
