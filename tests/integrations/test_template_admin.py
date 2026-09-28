"""Building and submitting WhatsApp templates.

A template is submitted once and reviewed by a human at Meta. A rejected one
keeps its name until somebody deletes it in the dashboard - the dev API has no
edit or delete - so the cost of a bad definition is manual work, not a retry.
These tests hold the rules that actually caused rejections.
"""

import pytest

from app.core.exceptions import ConfigurationError
from app.integrations.gallabox import template_admin as admin
from app.integrations.gallabox import templates as tpl
from app.integrations.gallabox.templates import TemplateSpec


def _spec(body: str, **kw) -> TemplateSpec:
    return TemplateSpec(
        name=kw.pop("name", "test_template"),
        category=kw.pop("category", "UTILITY"),
        trigger="test",
        params=kw.pop("params", ("customer_name",)),
        sample_body=body,
        **kw,
    )


# --- the body rules Meta enforces --------------------------------------------
def test_every_managed_template_passes_the_body_rules():
    """The whole catalogue must be submittable without hand-editing."""
    for spec in tpl.MANAGED_TEMPLATES:
        admin.check_body(spec)


def test_a_body_ending_in_a_variable_is_refused():
    with pytest.raises(ValueError, match="may not start or end with a variable"):
        admin.check_body(_spec("Your order is confirmed for {{1}}"))


def test_trailing_punctuation_does_not_rescue_a_trailing_variable():
    """The rejection that caused this check: '...delivery at {{4}}.'"""
    with pytest.raises(ValueError, match="may not start or end with a variable"):
        admin.check_body(_spec("Your order is confirmed for {{1}}."))


def test_a_body_starting_with_a_variable_is_refused():
    with pytest.raises(ValueError, match="may not start or end with a variable"):
        admin.check_body(_spec("{{1}}, your order is confirmed."))


def test_a_variable_with_words_after_it_is_fine():
    admin.check_body(_spec("Hi {{1}}, your order is on its way!"))


def test_components_refuses_a_bad_body_before_it_is_submitted():
    with pytest.raises(ValueError):
        admin.components(_spec("Ends on a variable {{1}}"))


# --- components ---------------------------------------------------------------
def test_body_example_carries_one_value_per_parameter():
    blocks = admin.components(tpl.PAYMENT_LINK)
    body = blocks[0]
    assert body["type"] == "BODY"
    assert len(body["example"]["body_text"][0]) == tpl.PAYMENT_LINK.param_count


def test_a_template_without_parameters_has_no_example():
    blocks = admin.components(_spec("Nothing to fill in here.", params=()))
    assert "example" in blocks[0] or blocks[0].get("example") is None
    assert blocks[0].get("example") is None or blocks[0]["example"]["body_text"][0] == []


def test_dynamic_url_button_points_at_the_pay_redirect(monkeypatch):
    monkeypatch.setattr(admin.settings, "pay_redirect_base_url",
                        "https://pay.example.com/", raising=False)
    buttons = admin.components(tpl.PAYMENT_LINK)[1]
    assert buttons["type"] == "BUTTONS"
    button = buttons["buttons"][0]
    assert button["type"] == "URL"
    assert button["text"] == "Pay Now"
    # The trailing slash must not survive into the URL.
    assert button["url"] == "https://pay.example.com/{{1}}"
    assert button["example"][0].startswith("https://pay.example.com/")


def test_a_dynamic_url_button_needs_a_configured_base(monkeypatch):
    monkeypatch.setattr(admin.settings, "pay_redirect_base_url", "", raising=False)
    with pytest.raises(ConfigurationError, match="PAY_REDIRECT_BASE_URL"):
        admin.components(tpl.PAYMENT_LINK)


def test_quick_reply_buttons_keep_their_order():
    buttons = admin.components(tpl.FEEDBACK_REQUEST)[1]["buttons"]
    assert [b["type"] for b in buttons] == ["QUICK_REPLY"] * 3
    assert [b["text"] for b in buttons] == ["Great", "Good", "Poor"]


def test_a_template_without_buttons_is_body_only():
    assert len(admin.components(tpl.ORDER_DELIVERED)) == 1


def test_the_welcome_template_offers_a_tappable_order_button():
    """Replaces "reply ORDER": a tap reaches the engine as a button payload."""
    assert tpl.WELCOME.button_kind == "quick_reply"
    buttons = admin.components(tpl.WELCOME)[1]["buttons"]
    assert buttons == [{"type": "QUICK_REPLY", "text": "Order Now"}]


def test_welcome_button_label_is_a_restart_keyword():
    """The button's own text is what a tap sends, so the engine must know it."""
    from app.services.conversation import prompts

    assert tpl.WELCOME.quick_replies[0].lower() in prompts.RESTART_KEYWORDS


# --- the creation payload ------------------------------------------------------
def test_creation_payload_targets_the_sending_channel(monkeypatch):
    monkeypatch.setattr(admin.settings, "gallabox_channel_id", "chan_1", raising=False)
    body = admin.creation_payload(tpl.ORDER_DELIVERED)
    assert body["channelId"] == "chan_1"
    assert body["name"] == "order_delivered"
    assert body["category"] == "UTILITY"
    assert body["allow_category_change"] is True


def test_creation_payload_language_matches_the_send_path():
    """A template approved as "en" cannot be sent as "en_US"."""
    from app.integrations.gallabox.messages import template_message

    sent = template_message("order_delivered", ["SHO-1"])
    assert admin.creation_payload(tpl.ORDER_DELIVERED)["language"] == \
        sent["template"]["language"]


def test_an_explicit_channel_overrides_the_default():
    body = admin.creation_payload(tpl.ORDER_DELIVERED, channel_id="chan_other")
    assert body["channelId"] == "chan_other"


# --- the account API ------------------------------------------------------------
@pytest.mark.asyncio
async def test_missing_account_id_is_a_clear_configuration_error(monkeypatch):
    monkeypatch.setattr(admin.settings, "gallabox_account_id", "", raising=False)
    with pytest.raises(ConfigurationError, match="GALLABOX_ACCOUNT_ID"):
        await admin.list_templates()


@pytest.mark.asyncio
async def test_list_templates_keeps_only_the_sending_channel(monkeypatch):
    monkeypatch.setattr(admin.settings, "gallabox_account_id", "acct_1", raising=False)
    monkeypatch.setattr(admin.settings, "gallabox_channel_id", "chan_1", raising=False)

    async def fake_get(path, **_kw):
        assert path == "/accounts/acct_1/whatsappTemplates"
        return [{"name": "ours", "channelId": "chan_1"},
                {"name": "someone_elses", "channelId": "chan_2"}]

    monkeypatch.setattr(admin.gallabox, "get", fake_get)
    rows = await admin.list_templates()
    assert [r["name"] for r in rows] == ["ours"]


@pytest.mark.asyncio
async def test_list_templates_can_return_every_channel(monkeypatch):
    monkeypatch.setattr(admin.settings, "gallabox_account_id", "acct_1", raising=False)

    async def fake_get(_path, **_kw):
        return [{"name": "a", "channelId": "chan_1"},
                {"name": "b", "channelId": "chan_2"}]

    monkeypatch.setattr(admin.gallabox, "get", fake_get)
    assert len(await admin.list_templates(channel_id="")) == 2


@pytest.mark.asyncio
async def test_create_template_posts_the_payload_to_the_account(monkeypatch):
    monkeypatch.setattr(admin.settings, "gallabox_account_id", "acct_1", raising=False)
    monkeypatch.setattr(admin.settings, "gallabox_channel_id", "chan_1", raising=False)
    seen: dict = {}

    async def fake_post(path, **kw):
        seen["path"] = path
        seen["json"] = kw["json"]
        return {"status": "pending", "whatsappTemplateId": "123"}

    monkeypatch.setattr(admin.gallabox, "post", fake_post)
    record = await admin.create_template(tpl.ORDER_DELIVERED)

    assert seen["path"] == "/accounts/acct_1/whatsappTemplates"
    assert seen["json"]["name"] == "order_delivered"
    assert record["status"] == "pending"


@pytest.mark.asyncio
async def test_a_non_dict_response_does_not_crash_the_caller(monkeypatch):
    """Gallabox has returned a bare string on error before now."""
    monkeypatch.setattr(admin.settings, "gallabox_account_id", "acct_1", raising=False)

    async def fake_post(_path, **_kw):
        return "unexpected"

    monkeypatch.setattr(admin.gallabox, "post", fake_post)
    assert await admin.create_template(tpl.ORDER_DELIVERED) == {}
