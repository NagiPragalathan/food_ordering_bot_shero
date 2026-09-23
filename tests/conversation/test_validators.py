"""Input validation and lead-stage recording."""

import pytest

from app.db.models import LeadStage
from app.services.conversation.prompts import SKIP_TOKENS
from app.services.conversation.validators import (
    clean_address,
    clean_email,
    clean_name,
    clean_phone,
    clean_postal_code,
    is_skip,
)
from app.services.customers import record_stage


@pytest.mark.parametrize("raw,expected", [
    ("Asha@Example.COM", "asha@example.com"),
    ("  asha.menon+tag@sub.example.co.uk  ", "asha.menon+tag@sub.example.co.uk"),
    ("<asha@example.com>", "asha@example.com"),
])
def test_valid_emails_are_normalised(raw, expected):
    assert clean_email(raw) == expected


@pytest.mark.parametrize("raw", ["", "nope", "nope@", "@example.com",
                                 "a b@example.com", None])
def test_invalid_emails_are_rejected(raw):
    assert clean_email(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("  Asha   Menon ", "Asha Menon"),
    ("Asha", "Asha"),
])
def test_valid_names_are_collapsed(raw, expected):
    assert clean_name(raw) == expected


@pytest.mark.parametrize("raw", ["", "x", "12345", "!!!", None])
def test_invalid_names_are_rejected(raw):
    assert clean_name(raw) is None


def test_phone_same_falls_back_to_the_whatsapp_number():
    assert clean_phone("same", fallback="+1 732 555 0142") == "17325550142"
    assert clean_phone("", fallback="17325550142") == "17325550142"


def test_phone_strips_formatting():
    assert clean_phone("(732) 555-0142") == "7325550142"


@pytest.mark.parametrize("raw", ["123", "1" * 16, "abc", None])
def test_invalid_phones_are_rejected(raw):
    assert clean_phone(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("08817", "08817"),
    ("sw1a 1aa", "SW1A 1AA"),
    ("  560001 ", "560001"),
])
def test_postal_codes_are_normalised(raw, expected):
    assert clean_postal_code(raw) == expected


@pytest.mark.parametrize("raw", ["", "!!", "a" * 12, None])
def test_invalid_postal_codes_are_rejected(raw):
    assert clean_postal_code(raw) is None


def test_address_needs_some_substance():
    assert clean_address("12 Maple Street, Edison") == "12 Maple Street, Edison"
    assert clean_address("abc") is None


def test_skip_tokens_are_recognised():
    assert is_skip("skip", SKIP_TOKENS)
    assert is_skip("  NONE ", SKIP_TOKENS)
    assert not is_skip("Apartment 4B", SKIP_TOKENS)


# --- lead stage --------------------------------------------------------------
def test_record_stage_stamps_the_transition(customer):
    assert record_stage(customer, LeadStage.CUISINE_SELECTED) is True
    assert customer.lead_stage == LeadStage.CUISINE_SELECTED
    assert str(LeadStage.CUISINE_SELECTED) in customer.stage_timestamps


def test_record_stage_ignores_a_repeat(customer):
    """Re-entering a stage keeps the original timestamp for drop-off reports."""
    record_stage(customer, LeadStage.CART_CREATED)
    first = customer.stage_timestamps[str(LeadStage.CART_CREATED)]

    assert record_stage(customer, LeadStage.CART_CREATED) is False
    assert customer.stage_timestamps[str(LeadStage.CART_CREATED)] == first


def test_stage_history_accumulates(customer):
    for stage in (LeadStage.DETAILS_CAPTURED, LeadStage.CUISINE_SELECTED,
                  LeadStage.CART_CREATED, LeadStage.OUTLET_SELECTED):
        record_stage(customer, stage)

    assert set(customer.stage_timestamps) >= {
        str(LeadStage.DETAILS_CAPTURED), str(LeadStage.CUISINE_SELECTED),
        str(LeadStage.CART_CREATED), str(LeadStage.OUTLET_SELECTED),
    }
