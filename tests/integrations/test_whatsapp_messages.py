"""WhatsApp payload builders and the 10 approved templates.

These guard the limits WhatsApp enforces silently - an over-long button title
is rejected at send time, which in production means a customer sees nothing.
"""

import pytest

from app.integrations.gallabox import templates as tpl
from app.integrations.gallabox.messages import (
    BUTTON_TITLE_LIMIT,
    MAX_LIST_ROWS,
    Button,
    ListRow,
    ListSection,
    button_message,
    clip,
    list_message,
    location_request_message,
    product_list_message,
    text_message,
)


# --- builders ----------------------------------------------------------------
def test_clip_shortens_and_marks_truncation():
    assert clip("short", 20) == "short"
    clipped = clip("a" * 40, BUTTON_TITLE_LIMIT)
    assert len(clipped) == BUTTON_TITLE_LIMIT
    assert clipped.endswith("…")


def test_text_message_shape():
    assert text_message("hi") == {"type": "text", "text": {"body": "hi"}}


def test_button_titles_are_clipped_to_the_whatsapp_limit():
    msg = button_message("Pick", [Button("id1", "A very long button label indeed")])
    title = msg["interactive"]["action"]["buttons"][0]["reply"]["title"]
    assert len(title) <= BUTTON_TITLE_LIMIT


@pytest.mark.parametrize("count", [0, 4])
def test_button_count_outside_one_to_three_is_rejected(count):
    buttons = [Button(f"id{i}", f"B{i}") for i in range(count)]
    with pytest.raises(ValueError, match="1-3 reply buttons"):
        button_message("Pick", buttons)


def test_three_buttons_is_allowed():
    msg = button_message("Review", [Button("c", "Confirm"),
                                    Button("e", "Edit"),
                                    Button("x", "Cancel")])
    assert len(msg["interactive"]["action"]["buttons"]) == 3


def test_list_rejects_more_than_ten_rows():
    rows = [ListRow(f"id{i}", f"Row {i}") for i in range(MAX_LIST_ROWS + 1)]
    with pytest.raises(ValueError, match="1-10 list rows"):
        list_message("Pick", [ListSection("All", rows)])


def test_list_row_description_is_included_only_when_present():
    msg = list_message("Pick", [ListSection("Outlets", [
        ListRow("a", "Shero Edison", "3.2 km away"),
        ListRow("b", "Shero JC"),
    ])])
    rows = msg["interactive"]["action"]["sections"][0]["rows"]
    assert rows[0]["description"] == "3.2 km away"
    assert "description" not in rows[1]


def test_location_request_uses_the_native_prompt():
    msg = location_request_message("Share your location")
    assert msg["interactive"]["type"] == "location_request_message"
    assert msg["interactive"]["action"]["name"] == "send_location"


def test_product_list_carries_the_catalog_id():
    msg = product_list_message(
        "cat_123",
        [{"title": "Andhra", "product_items": [{"product_retailer_id": "b1"}]}],
        header="Andhra menu", body="Browse and add to cart",
    )
    assert msg["interactive"]["action"]["catalog_id"] == "cat_123"


# --- templates ---------------------------------------------------------------
def test_all_ten_templates_are_registered():
    assert len(tpl.ALL_TEMPLATES) == 10
    assert len(tpl.BY_NAME) == 10


def test_template_names_match_the_spec():
    expected = {
        "payment_link", "payment_reminder", "payment_success", "payment_failed",
        "payment_expired", "refund_processed", "order_out_for_delivery",
        "order_delivered", "order_cancelled", "feedback_request",
    }
    assert set(tpl.BY_NAME) == expected


def test_declared_params_match_the_placeholders_in_the_body():
    """Every {{n}} in the sample body must have a declared parameter."""
    import re

    for spec in tpl.ALL_TEMPLATES:
        placeholders = {int(n) for n in re.findall(r"\{\{(\d+)\}\}", spec.sample_body)}
        expected = set(range(1, spec.param_count + 1))
        assert placeholders == expected, (
            f"{spec.name}: body uses {sorted(placeholders)} "
            f"but declares {sorted(expected)} ({spec.params})"
        )


def test_render_maps_values_to_positional_placeholders():
    payload = tpl.render(tpl.PAYMENT_LINK, "Asha", "SHO-260922-K4T9P",
                         "42.50", "Mon 6-7 PM", button_value="SHO-260922-K4T9P")
    body = payload["template"]["bodyValues"]
    assert body == {"1": "Asha", "2": "SHO-260922-K4T9P",
                    "3": "42.50", "4": "Mon 6-7 PM"}
    assert payload["template"]["buttonValues"] == {"0": ["SHO-260922-K4T9P"]}


def test_render_rejects_the_wrong_number_of_values():
    with pytest.raises(ValueError, match="expects 4 parameters"):
        tpl.render(tpl.PAYMENT_LINK, "Asha", button_value="x")


def test_dynamic_url_template_requires_a_button_value():
    with pytest.raises(ValueError, match="dynamic URL button"):
        tpl.render(tpl.PAYMENT_LINK, "Asha", "SHO-1", "10.00", "slot")


def test_template_without_a_button_omits_button_values():
    payload = tpl.render(tpl.ORDER_DELIVERED, "SHO-260922-K4T9P")
    assert "buttonValues" not in payload["template"]


def test_marketing_templates_are_the_two_the_spec_names():
    marketing = {s.name for s in tpl.ALL_TEMPLATES if s.category == "MARKETING"}
    assert marketing == {"payment_expired", "feedback_request"}
