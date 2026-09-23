"""The shared cart.

One customer has one cart, stored on the conversation. The WhatsApp handlers
and the web ordering page both write to it, so the properties worth pinning
are the ones that break when two writers disagree: merging, exact-quantity
updates being idempotent, and the cap holding on both paths.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.db.models import Conversation
from app.services import cart


@pytest.fixture
def conversation() -> Conversation:
    """A bare conversation row; the cart lives in its JSON context."""
    return Conversation(step="cuisine_menu", context={})


def _add(conversation, retailer_id="dish-a", name="Dish A", price="10.00", qty=1):
    return cart.add(conversation, retailer_id=retailer_id, name=name,
                    price=price, quantity=qty)


def test_a_new_cart_is_empty(conversation):
    assert cart.read(conversation) == []
    assert cart.item_count([]) == 0
    assert cart.subtotal([]) == Decimal("0.00")


def test_adding_the_same_dish_twice_merges_into_one_line(conversation):
    _add(conversation, qty=2)
    lines = _add(conversation, qty=3)
    assert len(lines) == 1
    assert lines[0]["quantity"] == 5


def test_different_dishes_stay_on_their_own_lines(conversation):
    _add(conversation, retailer_id="a", name="A")
    lines = _add(conversation, retailer_id="b", name="B")
    assert [l["retailer_id"] for l in lines] == ["a", "b"]


def test_adding_cannot_exceed_the_cap(conversation):
    _add(conversation, qty=18)
    lines = _add(conversation, qty=9)
    assert lines[0]["quantity"] == cart.MAX_QUANTITY


def test_set_quantity_is_exact_not_incremental(conversation):
    """The web + button sends a target, so repeats must be idempotent."""
    cart.set_quantity(conversation, retailer_id="a", name="A", price="5", quantity=3)
    lines = cart.set_quantity(conversation, retailer_id="a", name="A",
                              price="5", quantity=3)
    assert len(lines) == 1
    assert lines[0]["quantity"] == 3


def test_set_quantity_to_zero_removes_the_line(conversation):
    _add(conversation, retailer_id="a", name="A")
    lines = cart.set_quantity(conversation, retailer_id="a", name="A",
                              price="5", quantity=0)
    assert lines == []


def test_a_negative_quantity_removes_rather_than_storing_a_negative(conversation):
    _add(conversation, retailer_id="a", name="A")
    assert cart.set_quantity(conversation, retailer_id="a", name="A",
                             price="5", quantity=-4) == []


def test_set_quantity_respects_the_same_cap_as_adding(conversation):
    lines = cart.set_quantity(conversation, retailer_id="a", name="A",
                              price="5", quantity=999)
    assert lines[0]["quantity"] == cart.MAX_QUANTITY


def test_removing_one_dish_leaves_the_others(conversation):
    _add(conversation, retailer_id="a", name="A")
    _add(conversation, retailer_id="b", name="B")
    lines = cart.set_quantity(conversation, retailer_id="a", name="A",
                              price="5", quantity=0)
    assert [l["retailer_id"] for l in lines] == ["b"]


def test_clear_empties_the_cart(conversation):
    _add(conversation)
    cart.clear(conversation)
    assert cart.read(conversation) == []


def test_read_returns_copies_so_callers_cannot_mutate_the_stored_cart(conversation):
    _add(conversation, qty=2)
    lines = cart.read(conversation)
    lines[0]["quantity"] = 99
    assert cart.read(conversation)[0]["quantity"] == 2


def test_counts_and_subtotal(conversation):
    _add(conversation, retailer_id="a", name="A", price="7.41", qty=2)
    lines = _add(conversation, retailer_id="b", name="B", price="11.28", qty=1)
    assert cart.item_count(lines) == 3
    assert cart.subtotal(lines) == Decimal("26.10")


def test_a_corrupt_price_does_not_break_the_subtotal(conversation):
    """The badge must still render if a stored line is malformed."""
    conversation.set(cart=[
        {"retailer_id": "a", "name": "A", "quantity": 1, "unit_price": "oops"},
        {"retailer_id": "b", "name": "B", "quantity": 2, "unit_price": "5.00"},
    ])
    assert cart.subtotal(cart.read(conversation)) == Decimal("10.00")


def test_the_context_is_reassigned_so_sqlalchemy_persists_it(conversation):
    """Mutating the JSON dict in place would not mark the column dirty."""
    before = conversation.context
    _add(conversation)
    assert conversation.context is not before
