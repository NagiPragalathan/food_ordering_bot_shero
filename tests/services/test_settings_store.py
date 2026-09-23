"""Admin settings: encryption at rest, environment fallback, live override."""

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.models import AppSetting
from app.services.settings_store import (
    EDITABLE_KEYS,
    SETTING_GROUPS,
    SecretsUnavailable,
    apply_overrides,
    decrypt,
    encrypt,
    load_overrides,
    save,
    save_many,
    stored_keys,
)

# A valid Fernet key, for tests only.
TEST_KEY = "dGVzdC1rZXktZm9yLXVuaXQtdGVzdHMtMzIteWVzLWs="


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setattr(settings, "settings_encryption_key", TEST_KEY)


# --- encryption --------------------------------------------------------------
def test_round_trip():
    token = encrypt("sk_live_supersecret")
    assert token != "sk_live_supersecret"
    assert decrypt(token) == "sk_live_supersecret"


def test_ciphertext_differs_each_time():
    """Fernet includes a random IV, so equal secrets look different at rest."""
    assert encrypt("same") != encrypt("same")


def test_decrypt_returns_none_after_a_key_rotation(monkeypatch):
    """An unreadable value must fall back to the environment, not crash."""
    token = encrypt("secret")
    monkeypatch.setattr(settings, "settings_encryption_key",
                        "b3RoZXIta2V5LWZvci11bml0LXRlc3RzLTMyLXllcw==")
    assert decrypt(token) is None


def test_a_passphrase_is_accepted_and_derived():
    """Not everyone will paste a real Fernet key; derive one rather than fail."""
    from app.core.config import settings as live

    original = live.settings_encryption_key
    try:
        object.__setattr__(live, "settings_encryption_key", "just-a-passphrase")
        assert decrypt(encrypt("value")) == "value"
    finally:
        object.__setattr__(live, "settings_encryption_key", original)


def test_secrets_are_refused_without_a_key(monkeypatch):
    monkeypatch.setattr(settings, "settings_encryption_key", "")
    with pytest.raises(SecretsUnavailable):
        encrypt("secret")


# --- storage -----------------------------------------------------------------
async def test_secret_is_encrypted_in_the_table(session):
    await save(session, "STRIPE_SECRET_KEY", "sk_test_abc123", updated_by="me")
    await session.flush()

    row = await session.scalar(
        select(AppSetting).where(AppSetting.key == "STRIPE_SECRET_KEY"))
    assert row.is_secret is True
    assert "sk_test_abc123" not in (row.value or ""), "stored in the clear"
    assert decrypt(row.value) == "sk_test_abc123"


async def test_non_secret_is_stored_readable(session):
    await save(session, "ZOHO_DATA_CENTER", "in")
    await session.flush()

    row = await session.scalar(
        select(AppSetting).where(AppSetting.key == "ZOHO_DATA_CENTER"))
    assert row.is_secret is False
    assert row.value == "in"


async def test_blank_value_clears_the_override(session):
    await save(session, "ZOHO_DATA_CENTER", "in")
    await session.flush()
    await save(session, "ZOHO_DATA_CENTER", "")
    await session.flush()

    assert await stored_keys(session) == set()


async def test_unknown_key_is_refused(session):
    with pytest.raises(ValueError, match="not an editable setting"):
        await save(session, "DATABASE_URL", "postgres://somewhere-else")


async def test_load_overrides_decrypts(session):
    await save(session, "STRIPE_SECRET_KEY", "sk_test_xyz")
    await save(session, "ZOHO_DATA_CENTER", "eu")
    await session.flush()

    overrides = await load_overrides(session)
    assert overrides["STRIPE_SECRET_KEY"] == "sk_test_xyz"
    assert overrides["ZOHO_DATA_CENTER"] == "eu"


# --- applying ----------------------------------------------------------------
async def test_overrides_take_effect_on_the_live_settings(session, monkeypatch):
    monkeypatch.setattr(settings, "zoho_data_center", "com")
    await save(session, "ZOHO_DATA_CENTER", "in")
    await session.flush()

    applied = await apply_overrides(session)
    assert applied == 1
    assert settings.zoho_data_center == "in"
    # The derived host must follow, or every Zoho call would hit the wrong DC.
    assert settings.zoho_api_url == "https://www.zohoapis.in"


async def test_numeric_settings_are_coerced(session, monkeypatch):
    monkeypatch.setattr(settings, "payment_reminder_minutes", 15)
    await save(session, "PAYMENT_REMINDER_MINUTES", "20")
    await session.flush()
    await apply_overrides(session)

    assert settings.payment_reminder_minutes == 20
    assert isinstance(settings.payment_reminder_minutes, int)


async def test_blank_secret_in_a_form_keeps_the_existing_value(session):
    """The form never renders a secret back, so blank means 'unchanged'."""
    await save(session, "STRIPE_SECRET_KEY", "sk_test_original")
    await session.flush()

    changed = await save_many(session, {"STRIPE_SECRET_KEY": ""}, updated_by="me")
    assert changed == []

    overrides = await load_overrides(session)
    assert overrides["STRIPE_SECRET_KEY"] == "sk_test_original"


async def test_save_many_reports_only_real_changes(session, monkeypatch):
    monkeypatch.setattr(settings, "zoho_data_center", "com")
    monkeypatch.setattr(settings, "zoho_orders_module", "Orders")

    changed = await save_many(session, {
        "ZOHO_DATA_CENTER": "in",       # changed
        "ZOHO_ORDERS_MODULE": "Orders",  # same as current
    })
    assert changed == ["ZOHO_DATA_CENTER"]


# --- the editable surface ----------------------------------------------------
def test_every_grouped_field_is_editable():
    grouped = {key for fields in SETTING_GROUPS.values() for key, _, _ in fields}
    assert grouped == set(EDITABLE_KEYS)


def test_every_editable_key_is_a_real_setting():
    """A typo here would silently do nothing when saved."""
    unknown = [key for key in EDITABLE_KEYS if not hasattr(settings, key.lower())]
    assert unknown == []


def test_credentials_are_marked_secret():
    """Anything that grants access must be encrypted, not stored readable."""
    must_be_secret = {
        "GALLABOX_API_SECRET", "GALLABOX_WEBHOOK_TOKEN", "ZOHO_CLIENT_SECRET",
        "ZOHO_REFRESH_TOKEN", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
        "UBER_CLIENT_SECRET", "META_SYSTEM_USER_TOKEN", "GOOGLE_MAPS_API_KEY",
    }
    for key in must_be_secret:
        assert EDITABLE_KEYS[key][1] is True, f"{key} should be stored encrypted"


def test_database_url_is_not_editable_from_the_browser():
    """Repointing the database from a web form is not a thing we allow."""
    assert "DATABASE_URL" not in EDITABLE_KEYS
    assert "ADMIN_SESSION_SECRET" not in EDITABLE_KEYS
    assert "SETTINGS_ENCRYPTION_KEY" not in EDITABLE_KEYS
