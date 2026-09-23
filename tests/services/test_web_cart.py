"""The web storefront's cart sanitising and the dev delivery-fee fallback.

The cart arrives from a browser, so it is untrusted input: quantities, ids and
the shape of the list itself are all attacker-controlled. Prices are never
taken from it - that is covered in `test_pricing.py` - but a malformed line
must not reach the pricing code at all.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.api.routes.order_web import _clean_cart
from app.core.config import settings
from app.services.pricing import _fallback_fee


class TestCleanCart:
    def test_a_normal_cart_passes_through(self):
        assert _clean_cart([{"retailer_id": "a", "quantity": 2}]) == [
            {"retailer_id": "a", "quantity": 2}
        ]

    def test_the_page_may_send_id_instead_of_retailer_id(self):
        assert _clean_cart([{"id": "a", "quantity": 1}]) == [
            {"retailer_id": "a", "quantity": 1}
        ]

    @pytest.mark.parametrize("payload", [None, "", {}, 5, "[]"])
    def test_anything_that_is_not_a_list_is_an_empty_cart(self, payload):
        assert _clean_cart(payload) == []

    @pytest.mark.parametrize("line", [
        {"retailer_id": "", "quantity": 1},      # no dish
        {"retailer_id": "a", "quantity": 0},     # nothing ordered
        {"retailer_id": "a", "quantity": -3},    # negative, i.e. a refund attempt
        {"retailer_id": "a", "quantity": 21},    # above the per-line cap
        {"retailer_id": "a", "quantity": "two"},
        {"retailer_id": "a"},                    # missing quantity
        "not-a-dict",
    ])
    def test_malformed_lines_are_dropped(self, line):
        assert _clean_cart([line]) == []

    def test_one_bad_line_does_not_discard_the_good_ones(self):
        cart = _clean_cart([
            {"retailer_id": "good", "quantity": 2},
            {"retailer_id": "bad", "quantity": 999},
        ])
        assert cart == [{"retailer_id": "good", "quantity": 2}]

    def test_the_quantity_cap_matches_the_chat_flow(self):
        """20 is the limit the bot enforces; the web must not be looser."""
        assert _clean_cart([{"retailer_id": "a", "quantity": 20}])
        assert _clean_cart([{"retailer_id": "a", "quantity": 21}]) == []


class TestFallbackFee:
    """A guessed delivery fee must never reach a real customer."""

    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.setattr(settings, "delivery_fee_fallback", 0.0)
        monkeypatch.setattr(settings, "app_env", "development")
        assert _fallback_fee() is None

    def test_applies_in_development_when_set(self, monkeypatch):
        monkeypatch.setattr(settings, "delivery_fee_fallback", 5.99)
        monkeypatch.setattr(settings, "app_env", "development")
        assert _fallback_fee() == Decimal("5.99")

    def test_never_applies_in_production_even_when_set(self, monkeypatch):
        """The guard that matters: a misconfigured production deployment
        refuses the order rather than inventing a delivery charge."""
        monkeypatch.setattr(settings, "delivery_fee_fallback", 5.99)
        monkeypatch.setattr(settings, "app_env", "production")
        assert _fallback_fee() is None
