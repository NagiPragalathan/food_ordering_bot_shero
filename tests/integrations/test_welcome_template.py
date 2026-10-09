"""The welcome template: Order Now (the shop's website) and Continue on WhatsApp.

A plain WhatsApp message cannot carry a link button and a reply button
together, so the greeting is a template; until it is approved, the fallback
puts the link in the text and keeps the reply button.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone

import pytest

from app.integrations.gallabox import fallback, template_admin
from app.integrations.gallabox import templates as tpl

APPROVED_LONG_AGO = "2026-09-24T08:41:31.739Z"


@pytest.mark.parametrize("spec", [tpl.WELCOME_BACK, tpl.WELCOME_NEW])
def test_the_template_has_the_website_link_then_continue_on_whatsapp(spec):
    buttons = template_admin.components(spec)[-1]["buttons"]
    assert buttons == [
        {"type": "URL", "text": "Order Now", "url": "https://www.shero.us/"},
        {"type": "QUICK_REPLY", "text": "Continue on WhatsApp"},
    ]
    assert spec.category == "UTILITY"     # Meta does not deliver marketing to US numbers


def test_it_needs_only_the_name_to_send():
    block = tpl.render(tpl.WELCOME_BACK, "Nagi")["template"]
    assert block["templateName"] == "shero_welcome_back"
    assert block["bodyValues"] == {"1": "Nagi"}
    assert "buttonValues" not in block            # the link is fixed, nothing to fill in
    with pytest.raises(ValueError):
        tpl.render(tpl.WELCOME_BACK)


def test_before_approval_the_link_goes_in_the_text_and_the_button_stays():
    interactive = fallback.message(tpl.WELCOME_BACK, ("Nagi",), None)["interactive"]
    assert interactive["body"]["text"] == (
        "Welcome back to Shero Home Food, Nagi! \U0001F44B\n\n"
        "Order Now: https://www.shero.us/")
    # The tap answers exactly as the template's quick reply would.
    assert interactive["action"]["buttons"] == [
        {"type": "reply", "reply": {"id": "Continue on WhatsApp",
                                    "title": "Continue on WhatsApp"}}]


@pytest.fixture
def usable():
    import app.integrations.gallabox.template_status as module
    return importlib.reload(module).usable


def _row(**extra) -> dict:
    return {"name": "shero_welcome_back", "status": "approved", "category": "UTILITY",
            "statusUpdatedAt": APPROVED_LONG_AGO,
            "components": [{"type": "BUTTONS", "buttons": [
                {"type": "URL", "text": "Order Now", "url": "https://www.shero.us/"},
                {"type": "QUICK_REPLY", "text": "Continue on WhatsApp"}]}],
            **extra}


NOW = datetime(2026, 10, 7, 7, 0, tzinfo=timezone.utc)


def test_a_fixed_link_to_the_website_is_not_an_outdated_link(usable):
    assert usable(_row(), NOW) is True


def test_a_template_meta_refiled_as_marketing_is_not_used(usable):
    """Marketing templates are not delivered to US numbers; the fallback is."""
    assert usable(_row(category="MARKETING"), NOW) is False
