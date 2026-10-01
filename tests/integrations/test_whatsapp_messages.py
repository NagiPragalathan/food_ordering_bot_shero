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
def test_the_specs_ten_templates_are_registered():
    assert len(tpl.ALL_TEMPLATES) == 10


def test_managed_templates_add_the_welcome_opener_and_order_summary():
    """ALL_TEMPLATES is the spec's ten; MANAGED is what we create on the WABA."""
    assert set(tpl.MANAGED_TEMPLATES) - set(tpl.ALL_TEMPLATES) == {
        tpl.WELCOME, tpl.ORDER_SUMMARY, tpl.MENU_LINK}
    assert len(tpl.BY_NAME) == len(tpl.MANAGED_TEMPLATES) == 13


def test_template_names_match_the_spec():
    # The templates with a link button are the _v2 versions, which point at
    # the hosted address (see templates.RENAMED).
    expected = {
        "shero_payment_link_v2", "shero_payment_reminder_v2", "payment_confirmed_v2",
        "shero_payment_failed_v2",
        "payment_expired_v2", "refund_processed", "order_out_for_delivery",
        "order_delivered", "order_cancelled", "shero_feedback",
    }
    assert {s.name for s in tpl.ALL_TEMPLATES} == expected
    assert set(tpl.BY_NAME) == expected | {"shero_welcome_message", "order_summary_v2",
                                           "menu_link_v2"}


def test_declared_params_match_the_placeholders_in_the_body():
    """Every {{n}} in the sample body must have a declared parameter."""
    import re

    for spec in tpl.MANAGED_TEMPLATES:
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
    # A list, in Gallabox's shape: a dict keyed by index is rejected with a 500.
    assert payload["template"]["buttonValues"] == [{
        "index": 0, "sub_type": "url",
        "parameters": {"type": "text", "text": "SHO-260922-K4T9P"}}]


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
    assert marketing == {"payment_expired_v2", "shero_feedback"}
    # The welcome opener is promotional too, and is not part of the spec's ten.
    assert tpl.WELCOME.category == "MARKETING"


def test_order_summary_has_pay_now_then_two_quick_replies(monkeypatch):
    from app.core.config import settings
    from app.integrations.gallabox import template_admin

    monkeypatch.setattr(settings, "pay_redirect_base_url", "https://shero.test/pay")
    blocks = template_admin.components(tpl.ORDER_SUMMARY)
    buttons = blocks[-1]["buttons"]

    assert [b["type"] for b in buttons] == ["URL", "QUICK_REPLY", "QUICK_REPLY"]
    assert buttons[0]["url"] == "https://shero.test/pay/{{1}}"
    assert [b["text"] for b in buttons[1:]] == ["Change menu", "Update location"]


def test_order_summary_quick_replies_are_what_the_bot_listens_for():
    """A tapped quick reply comes back as its own label."""
    from app.services.conversation import prompts as p

    change, update = (label.lower() for label in tpl.ORDER_SUMMARY.quick_replies)
    assert change in p.CHANGE_MENU_KEYWORDS
    assert update in p.UPDATE_LOCATION_KEYWORDS


def test_order_summary_sends_the_pay_now_suffix_as_button_zero():
    payload = tpl.render(tpl.ORDER_SUMMARY, "Asha", "SHO-1", "2 x Sambar",
                         "12 Maple St, 08820", "Fri 7-8 PM", "29.42",
                         button_value="SHO-1")
    assert payload["template"]["buttonValues"][0]["index"] == 0
    assert payload["template"]["buttonValues"][0]["parameters"]["text"] == "SHO-1"


def test_menu_link_points_view_menu_at_the_ordering_page(monkeypatch):
    from app.core.config import settings
    from app.integrations.gallabox import template_admin

    monkeypatch.setattr(settings, "public_base_url", "https://shero.test")
    buttons = template_admin.components(tpl.MENU_LINK)[-1]["buttons"]

    assert buttons[0] == {**buttons[0], "type": "URL", "text": "View Menu",
                          "url": "https://shero.test/order/{{1}}"}
    assert buttons[1] == {"type": "QUICK_REPLY", "text": "Get new link"}


def test_menu_link_refuses_a_localhost_base(monkeypatch):
    """The URL is baked into the approved template; localhost would be useless."""
    import pytest
    from app.core.config import settings
    from app.core.exceptions import ConfigurationError
    from app.integrations.gallabox import template_admin

    monkeypatch.setattr(settings, "public_base_url", "http://localhost:8000")
    with pytest.raises(ConfigurationError):
        template_admin.components(tpl.MENU_LINK)
