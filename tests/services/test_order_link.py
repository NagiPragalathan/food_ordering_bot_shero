"""Signed ordering links.

The token is the only thing standing between a URL and somebody else's saved
address, so the failure modes worth pinning are: a tampered token, an expired
one, and a token signed with a different secret.
"""

from __future__ import annotations

import uuid

import pytest
from itsdangerous import URLSafeTimedSerializer

from app.core.config import settings
from app.services import order_link


@pytest.fixture(autouse=True)
def signing_secret(monkeypatch):
    monkeypatch.setattr(settings, "admin_session_secret", "test-signing-secret")
    monkeypatch.setattr(settings, "public_base_url", "https://api.example.com")


def test_a_token_round_trips_to_the_same_customer():
    customer_id = uuid.uuid4()
    assert order_link.read_token(order_link.build_token(customer_id)) == customer_id


def test_the_url_points_at_the_storefront_route():
    url = order_link.build_url(uuid.uuid4())
    assert url.startswith("https://api.example.com/order/")


def test_a_trailing_slash_on_the_base_url_does_not_double_up(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://api.example.com/")
    assert "//order/" not in order_link.build_url(uuid.uuid4())


def test_a_tampered_token_is_rejected():
    token = order_link.build_token(uuid.uuid4())
    # Flip the payload but keep the structure; the signature no longer matches.
    broken = ("B" if token[0] != "B" else "C") + token[1:]
    assert order_link.read_token(broken) is None


def test_a_token_signed_with_another_secret_is_rejected(monkeypatch):
    other = URLSafeTimedSerializer("a-different-secret", salt=order_link.SALT)
    assert order_link.read_token(other.dumps(str(uuid.uuid4()))) is None


def test_an_expired_token_is_rejected(monkeypatch):
    token = order_link.build_token(uuid.uuid4())
    monkeypatch.setattr(order_link, "MAX_AGE_SECONDS", -1)
    assert order_link.read_token(token) is None


def test_rubbish_is_rejected_rather_than_raising():
    for value in ["", "not-a-token", "a.b.c", "x" * 200]:
        assert order_link.read_token(value) is None


def test_a_validly_signed_non_uuid_is_still_rejected():
    """Signed does not mean well-formed - the payload is checked too."""
    signer = URLSafeTimedSerializer("test-signing-secret", salt=order_link.SALT)
    assert order_link.read_token(signer.dumps("definitely-not-a-uuid")) is None


def test_signing_without_a_secret_fails_loudly(monkeypatch):
    """Silently unsigned links would be worse than no links."""
    monkeypatch.setattr(settings, "admin_session_secret", "")
    monkeypatch.setattr(settings, "settings_encryption_key", "")
    with pytest.raises(RuntimeError):
        order_link.build_url(uuid.uuid4())
