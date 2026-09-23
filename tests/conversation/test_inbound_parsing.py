"""Inbound webhook parsing.

The parser must never raise on an unexpected envelope - an exception here
would make Gallabox retry the same bad payload forever.
"""

from app.schemas.inbound import InboundKind, parse_inbound


def envelope(whatsapp: dict, *, phone: str = "+1 (732) 555-0142",
             message_id: str = "msg-1", name: str = "Asha") -> dict:
    return {
        "event": "Message.received",
        "payload": {
            "id": message_id,
            "contact": {"phone": phone, "name": name},
            "whatsapp": whatsapp,
        },
    }


def test_text_message_is_trimmed_and_number_normalised():
    event = parse_inbound(envelope({"type": "text", "text": {"body": "  hello  "}}))
    assert event.kind is InboundKind.TEXT
    assert event.text == "hello"
    assert event.whatsapp_number == "17325550142"
    assert event.contact_name == "Asha"
    assert event.is_actionable


def test_click_to_whatsapp_referral_is_captured():
    event = parse_inbound(envelope({
        "type": "text",
        "text": {"body": "hi"},
        "referral": {"source_id": "ad_99", "campaign_id": "camp_7"},
    }))
    assert event.ad_id == "ad_99"
    assert event.campaign_id == "camp_7"
    assert event.referral["source_id"] == "ad_99"


def test_list_reply_becomes_a_choice():
    event = parse_inbound(envelope({
        "type": "interactive",
        "interactive": {"list_reply": {"id": "cuisine:andhra", "title": "Andhra"}},
    }))
    assert event.kind is InboundKind.REPLY
    assert event.choice == "cuisine:andhra"
    assert event.reply_title == "Andhra"


def test_button_reply_becomes_a_choice():
    event = parse_inbound(envelope({
        "type": "interactive",
        "interactive": {"button_reply": {"id": "menu:order", "title": "Order Online"}},
    }))
    assert event.choice == "menu:order"


def test_camel_case_reply_keys_are_accepted():
    """Some webhook versions send buttonReply rather than button_reply."""
    event = parse_inbound(envelope({
        "type": "interactive",
        "interactive": {"buttonReply": {"id": "menu:talk", "title": "Talk to Us"}},
    }))
    assert event.choice == "menu:talk"


def test_template_quick_reply_arrives_as_a_button_type():
    event = parse_inbound(envelope({
        "type": "button",
        "button": {"payload": "rating:great", "text": "Great"},
    }))
    assert event.kind is InboundKind.REPLY
    assert event.choice == "rating:great"


def test_location_pin_is_parsed():
    event = parse_inbound(envelope({
        "type": "location",
        "location": {"latitude": 40.5187, "longitude": -74.4121},
    }))
    assert event.kind is InboundKind.LOCATION
    assert event.latitude == 40.5187
    assert event.longitude == -74.4121


def test_cart_lines_are_parsed_with_quantities():
    event = parse_inbound(envelope({
        "type": "order",
        "order": {"product_items": [
            {"product_retailer_id": "biryani", "quantity": 2, "item_price": "12.50"},
            {"product_retailer_id": "appam", "quantity": 1},
        ]},
    }))
    assert event.kind is InboundKind.CART
    assert [(line.retailer_id, line.quantity) for line in event.cart_lines] == [
        ("biryani", 2), ("appam", 1),
    ]


def test_cart_lines_without_a_retailer_id_are_skipped():
    event = parse_inbound(envelope({
        "type": "order",
        "order": {"product_items": [{"quantity": 3}, {"product_retailer_id": "ok"}]},
    }))
    assert len(event.cart_lines) == 1


def test_unknown_envelope_degrades_instead_of_raising():
    event = parse_inbound({"totally": "unexpected"})
    assert event.kind is InboundKind.UNKNOWN
    assert event.is_actionable is False


def test_empty_body_does_not_raise():
    event = parse_inbound({})
    assert event.is_actionable is False


def test_message_without_a_number_is_not_actionable():
    event = parse_inbound(envelope({"type": "text", "text": {"body": "hi"}},
                                   phone=""))
    assert event.is_actionable is False


def test_bad_coordinates_become_none_rather_than_crashing():
    event = parse_inbound(envelope({
        "type": "location",
        "location": {"latitude": "not-a-number", "longitude": None},
    }))
    assert event.kind is InboundKind.LOCATION
    assert event.latitude is None
    assert event.longitude is None
