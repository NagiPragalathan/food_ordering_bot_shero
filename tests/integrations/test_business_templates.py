"""Each business's own copies of the WhatsApp templates.

Shero US and DST India are two numbers in one Gallabox account, where a
template name is unique. DST India's copy of each catalogue template is the
same message named "dst_" + the name, its amounts in rupees, its buttons on
its own /dst links, created on its own number. Shero US is exactly as before.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone

import pytest

from app.core import tenants
from app.core.config import settings_for
from app.integrations.gallabox import fallback, template_admin
from app.integrations.gallabox import templates as tpl

DST_CHANNEL = "6af30a65a7aa4cd16b22569a"
US_CHANNEL = "6a2fee09a806dc48e9e4eef7"
INDIA_FLAG = "\U0001F1EE\U0001F1F3"


@pytest.fixture
def dst(monkeypatch):
    own = settings_for(tenants.DST)
    monkeypatch.setattr(own, "gallabox_channel_id", DST_CHANNEL)
    monkeypatch.setattr(own, "gallabox_account_id", "acc")
    monkeypatch.setattr(own, "public_base_url", "https://smo.shero.us/dst")
    monkeypatch.setattr(own, "pay_redirect_base_url", "https://smo.shero.us/dst/pay")
    with tenants.use(tenants.DST):
        yield own


@pytest.fixture
def us(monkeypatch):
    own = settings_for(tenants.US)
    monkeypatch.setattr(own, "gallabox_channel_id", US_CHANNEL)
    monkeypatch.setattr(own, "gallabox_account_id", "acc")
    monkeypatch.setattr(own, "public_base_url", "https://smo.shero.us")
    monkeypatch.setattr(own, "pay_redirect_base_url", "https://smo.shero.us/pay")
    return own


# --- names ---------------------------------------------------------------------------
def test_shero_us_names_are_unchanged(us):
    assert tpl.wire_name(tpl.PAYMENT_LINK) == "shero_payment_link_v2"
    assert tpl.for_business(tpl.ORDER_SUMMARY) is tpl.ORDER_SUMMARY
    assert tpl.managed() == tpl.MANAGED_TEMPLATES
    block = tpl.render(tpl.PAYMENT_SUCCESS, "SHO-1", "42.50", "Shero", "Wed")
    assert block["template"]["templateName"] == "payment_confirmed_v5"


def test_dst_india_sends_its_own_prefixed_copy(dst):
    assert tpl.wire_name(tpl.PAYMENT_LINK) == "dst_shero_payment_link_v2"
    assert tpl.wire_name("order_summary_v2") == "dst_order_summary_v2"
    block = tpl.render(tpl.PAYMENT_SUCCESS, "DST-1", "749.00", "Shero Anna Nagar", "Wed")
    assert block["template"]["templateName"] == "dst_payment_confirmed_v5"
    delivered = tpl.render(tpl.ORDER_DELIVERED, "DST-1", button_value="tok")
    assert delivered["template"]["templateName"] == "dst_order_delivered_v2"
    assert [s.name for s in tpl.managed()] == [f"dst_{s.name}" for s in tpl.MANAGED_TEMPLATES]


def test_only_catalogue_names_are_prefixed(dst):
    """A nudge message, an SMO template or DST's own F1-F12 already carry
    their full name."""
    for name in ("dst_rnr_1", "dst_nudge_cart", "smo_follow_up", "dst_win_back"):
        assert tpl.wire_name(name) == name
    assert tpl.catalogue_name("dst_order_summary_v2") == "order_summary_v2"
    assert tpl.catalogue_name("dst_rnr_1") == "dst_rnr_1"


def test_the_prefix_is_never_doubled(dst):
    copy = tpl.for_business(tpl.PAYMENT_LINK)
    assert tpl.for_business(copy) is copy
    assert tpl.wire_name(copy.name) == copy.name


# --- bodies --------------------------------------------------------------------------
def test_dst_bodies_are_in_rupees_with_india_s_flag(dst):
    assert "₹{{3}}" in tpl.for_business(tpl.PAYMENT_LINK).sample_body
    assert "₹{{6}}" in tpl.for_business(tpl.ORDER_SUMMARY).sample_body
    assert "$" not in tpl.for_business(tpl.ORDER_ALERT).sample_body
    welcome = tpl.for_business(tpl.WELCOME).sample_body
    assert INDIA_FLAG in welcome and tpl.US_FLAG not in welcome


def test_us_bodies_keep_dollars(us):
    assert "${{3}}" in tpl.for_business(tpl.PAYMENT_LINK).sample_body
    assert tpl.US_FLAG in tpl.for_business(tpl.WELCOME).sample_body


def test_the_text_fallback_shows_the_business_currency(dst):
    text = fallback.body_text(tpl.PAYMENT_LINK, ("Asha", "DST-1", "749.00", "Wed 7 PM"))
    assert "₹749.00" in text and "$" not in text
    message = fallback.message(tpl.PAYMENT_LINK, ("Asha", "DST-1", "749.00", "Wed"), "DST-1")
    assert message["interactive"]["action"]["parameters"]["url"] == \
        "https://smo.shero.us/dst/pay/DST-1"


# --- submitting them -----------------------------------------------------------------
def test_dst_submission_goes_to_its_own_number_with_its_own_links(dst):
    payload = template_admin.creation_payload(tpl.PAYMENT_LINK)
    assert payload["name"] == "dst_shero_payment_link_v2"
    assert payload["channelId"] == DST_CHANNEL
    body, buttons = payload["components"][0], payload["components"][-1]["buttons"]
    assert "₹{{3}}" in body["text"]
    assert buttons[0]["url"] == "https://smo.shero.us/dst/pay/{{1}}"
    assert buttons[0]["example"][0].startswith("https://smo.shero.us/dst/pay/DST-")
    # The reviewer sees Indian examples.
    assert body["example"]["body_text"][0][2] == "749.00"


def test_dst_menu_and_invoice_buttons_point_at_its_pages(dst):
    menu = template_admin.components(tpl.MENU_LINK)[-1]["buttons"][0]["url"]
    invoice = template_admin.components(tpl.ORDER_DELIVERED)[-1]["buttons"][0]["url"]
    assert menu == "https://smo.shero.us/dst/order/{{1}}"
    assert invoice == "https://smo.shero.us/dst/receipt/{{1}}"


def test_us_submission_is_as_before(us):
    payload = template_admin.creation_payload(tpl.PAYMENT_LINK)
    assert payload["name"] == "shero_payment_link_v2" and payload["channelId"] == US_CHANNEL
    assert payload["components"][-1]["buttons"][0]["url"] == "https://smo.shero.us/pay/{{1}}"


async def test_create_template_posts_the_dst_copy(dst, monkeypatch):
    posted = []

    async def post(path, json=None, **_):
        posted.append((path, json))
        return {"status": "pending"}

    monkeypatch.setattr(template_admin.gallabox, "post", post)
    await template_admin.create_template(tpl.REFUND_PROCESSED)
    [(path, body)] = posted
    assert path == "/accounts/acc/whatsappTemplates"
    assert (body["name"], body["channelId"]) == ("dst_refund_processed", DST_CHANNEL)


def test_the_submit_script_shows_dst_copies(dst, capsys):
    from scripts import submit_templates

    assert submit_templates.main(["--tenant", "dst", "--json"]) == 0
    out = capsys.readouterr().out
    assert '"name": "dst_order_summary_v2"' in out and '"name": "order_summary_v2"' not in out


# --- the approval check --------------------------------------------------------------
NOW = datetime(2026, 9, 28, 7, 0, tzinfo=timezone.utc)
LONG_AGO = "2026-09-24T08:41:31.739Z"


@pytest.fixture
def status(monkeypatch):
    """The real approval check (not the conftest stub), Gallabox faked."""
    import app.integrations.gallabox.template_status as module

    real = importlib.reload(module)
    real.clear_cache()
    rows: list[dict] = []

    async def listing():
        return rows

    async def clock():
        return NOW

    monkeypatch.setattr(template_admin, "list_templates", listing)
    monkeypatch.setattr(real, "gallabox_now", clock)
    yield real, rows
    real.clear_cache()


async def test_the_approval_check_asks_for_the_business_own_copy(status, dst):
    module, rows = status
    rows += [{"name": "order_summary_v2", "status": "approved", "statusUpdatedAt": LONG_AGO}]
    # Shero US's template is approved, DST India's copy is not there yet.
    assert await module.is_approved(tpl.ORDER_SUMMARY.name) is False
    rows.append({"name": "dst_order_summary_v2", "status": "approved",
                 "statusUpdatedAt": LONG_AGO})
    module.clear_cache()
    assert await module.is_approved(tpl.ORDER_SUMMARY.name) is True


async def test_a_dst_copy_meta_made_marketing_is_not_usable(status, dst):
    module, rows = status
    rows.append({"name": "dst_order_summary_v2", "status": "approved", "category": "MARKETING",
                 "statusUpdatedAt": LONG_AGO})
    assert await module.is_approved(tpl.ORDER_SUMMARY.name) is False


async def test_us_approval_check_is_unchanged(status, us):
    module, rows = status
    rows.append({"name": "order_summary_v2", "status": "approved", "statusUpdatedAt": LONG_AGO})
    assert await module.is_approved(tpl.ORDER_SUMMARY.name) is True


async def test_each_business_keeps_its_own_approval_cache(status, us):
    module, rows = status
    rows.append({"name": "order_summary_v2", "status": "approved", "statusUpdatedAt": LONG_AGO})
    assert await module.is_approved(tpl.ORDER_SUMMARY.name) is True
    with tenants.use(tenants.DST):
        assert await module.is_approved(tpl.ORDER_SUMMARY.name) is False
