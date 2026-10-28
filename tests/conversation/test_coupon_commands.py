"""Coupon codes typed in the WhatsApp chat (handlers/coupons.py).

Walks the real chat flow (the happy-path harness): 2 x Drumstick Sambar is
$25.00 of dishes, delivery $5.99 + $1.20 fees, so $32.19 with no coupon.
"""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from app.db.models import (ConversationStep, DiscountType, Order, OrderStage, Placement,
                           Promotion, SlotHold, SlotHoldStatus)
from app.services import promotions
from app.services.conversation import prompts as p
from app.services.conversation.engine import handle_event
from tests.conversation.test_happy_path import (NEARBY, _add_drumstick, _sign_up, _state,
                                                bot, pin, reply, text)


async def _promo(session, code, **values):
    promotion = Promotion(title=f"{code} offer", code=code, is_active=True,
                          **{"discount_type": DiscountType.FLAT, "discount_value": Decimal("5"),
                             "placement": Placement.CHECKOUT, **values})
    session.add(promotion)
    await session.flush()
    return promotion


async def _to_summary(session, bot):
    await _sign_up(session, bot)
    await _add_drumstick(session, bot)
    await handle_event(session, reply("cart:checkout"))
    await handle_event(session, pin(*NEARBY))
    await handle_event(session, text("12 Maple Street, Edison"))
    await handle_event(session, text("skip"))
    await handle_event(session, text("skip"))
    await handle_event(session, text("same"))
    await handle_event(session, reply(bot.row_id("slot:")))


async def _orders(session):
    return list((await session.execute(select(Order).order_by(Order.created_at))).scalars())


async def test_offers_lists_what_this_customer_can_use(session, outlet, menu, bot):
    await _promo(session, "SAVE5", per_customer_limit=1)
    await _promo(session, "BIG10", min_order=Decimal("40"))
    await _promo(session, "SECRET", placement=Placement.HIDDEN)
    await _promo(session, "OLD", ends_on=(await promotions.local_today(session, outlet))
                 - timedelta(days=1))
    await _sign_up(session, bot)
    await _add_drumstick(session, bot)

    await handle_event(session, text("offers"))

    body = bot.last().body
    assert "*SAVE5* - $5 off (Once per customer)" in body
    assert "Add $15.00 more to use BIG10" in body
    assert "SECRET" not in body and "OLD" not in body          # hidden, expired
    assert "*apply SAVE5*" in body


async def test_a_code_typed_at_the_summary_reprices_on_the_same_slot(session, outlet, menu, bot):
    await _promo(session, "SAVE5")
    await _to_summary(session, bot)
    first = bot.last()
    assert "*Total: $32.19*" in first.body
    assert first.payload["footer"] == p.SUMMARY_COUPON_HINT
    _, conversation = await _state(session)
    slot_id = conversation.get("slot_id")

    await handle_event(session, text("save5"))

    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.AWAIT_SUMMARY_CONFIRM
    assert conversation.get("slot_id") == slot_id
    assert any("*SAVE5* applied: $5 off" in m.body for m in bot.sent)
    summary = bot.last()
    assert "*Total: $27.19*" in summary.body and summary.payload["footer"] is None

    old, new = await _orders(session)
    assert old.stage == OrderStage.CANCELLED and new.promo_code == "SAVE5"
    holds = list((await session.execute(select(SlotHold))).scalars())
    assert [h.order_id for h in holds if h.status != SlotHoldStatus.RELEASED] == [new.id]

    await handle_event(session, reply("summary:confirm"))
    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.AWAIT_PAYMENT
    assert (await _orders(session))[-1].total == Decimal("27.19")


async def test_removing_the_coupon_at_the_summary_puts_the_total_back(session, outlet, menu, bot):
    await _promo(session, "SAVE5")
    await _to_summary(session, bot)
    await handle_event(session, text("apply SAVE5"))
    assert "*Total: $27.19*" in bot.last().body

    await handle_event(session, text("remove coupon"))

    assert any("*SAVE5* removed" in m.body for m in bot.sent)
    assert "*Total: $32.19*" in bot.last().body
    assert bot.last().payload["footer"] == p.SUMMARY_COUPON_HINT
    _, conversation = await _state(session)
    assert promotions.stored_code(conversation) is None
    assert [o.stage for o in await _orders(session)].count(OrderStage.CANCELLED) == 2


async def test_a_code_applied_while_ordering_is_on_the_summary(session, outlet, menu, bot):
    await _promo(session, "SAVE5")
    await _sign_up(session, bot)
    await _add_drumstick(session, bot)

    await handle_event(session, text("coupon code save5"))
    assert bot.last().body == ("*SAVE5* applied: $5 off. "
                               "You will see the saving on your order summary.")

    await handle_event(session, reply("cart:checkout"))
    await handle_event(session, pin(*NEARBY))
    await handle_event(session, text("12 Maple Street, Edison"))
    await handle_event(session, text("skip"))
    await handle_event(session, text("skip"))
    await handle_event(session, text("same"))
    await handle_event(session, reply(bot.row_id("slot:")))
    assert "*Total: $27.19*" in bot.last().body


async def test_a_cart_below_the_minimum_is_told_how_much_more(session, outlet, menu, bot):
    await _promo(session, "BIG10", min_order=Decimal("40"))
    await _sign_up(session, bot)
    await _add_drumstick(session, bot)

    await handle_event(session, text("apply big10"))

    assert "Add $15.00 more to use BIG10 (minimum order $40.00)." in bot.last().body
    _, conversation = await _state(session)
    assert promotions.stored_code(conversation) == "BIG10"      # works once it qualifies


async def test_codes_that_cannot_be_used_are_explained(session, outlet, menu, bot):
    today = await promotions.local_today(session, outlet)
    await _promo(session, "OLD", ends_on=today - timedelta(days=1))
    await _sign_up(session, bot)

    await handle_event(session, text("apply NOPE"))
    assert "NOPE is not a valid code" in bot.last().body
    await handle_event(session, text("apply old"))
    assert "OLD has expired." in bot.last().body and "Type *offers*" in bot.last().body
    _, conversation = await _state(session)
    assert promotions.stored_code(conversation) is None


async def test_no_change_once_the_payment_link_is_out(session, outlet, menu, bot):
    await _promo(session, "SAVE5")
    await _to_summary(session, bot)
    await handle_event(session, reply("summary:confirm"))

    await handle_event(session, text("apply SAVE5"))

    assert "already out" in bot.last().body and "*change menu*" in bot.last().body
    _, conversation = await _state(session)
    assert promotions.stored_code(conversation) is None
    assert (await _orders(session))[-1].total == Decimal("32.19")


async def test_an_ordinary_word_at_the_summary_is_not_a_coupon(session, outlet, menu, bot):
    await _promo(session, "SAVE5")
    await _to_summary(session, bot)

    await handle_event(session, text("maybe"))

    assert bot.last().body == p.FALLBACK
    assert len(await _orders(session)) == 1


async def test_a_code_typed_as_the_address_is_answered_as_an_address(session, outlet, menu, bot):
    await _promo(session, "SAVE5")
    await _sign_up(session, bot)
    await _add_drumstick(session, bot)
    await handle_event(session, reply("cart:checkout"))
    await handle_event(session, pin(*NEARBY))

    await handle_event(session, text("SAVE5"))

    # The address step judges it (too short to be an address), not coupons.
    _, conversation = await _state(session)
    assert conversation.step == ConversationStep.AWAIT_ADDRESS
    assert "applied" not in bot.last().body
    assert promotions.stored_code(conversation) is None


async def _with_points(session, bot, *, stack: bool):
    """A customer with 1000 points who asked to use 200, at the summary."""
    from tests.services.test_loyalty import configure, grant

    await configure(session, stack_with_coupons=stack)
    await _sign_up(session, bot)
    customer, _ = await _state(session)
    await grant(session, customer)
    await _add_drumstick(session, bot)
    await handle_event(session, text("redeem 200"))
    await handle_event(session, reply("cart:checkout"))
    await handle_event(session, pin(*NEARBY))
    await handle_event(session, text("12 Maple Street, Edison"))
    await handle_event(session, text("skip"))
    await handle_event(session, text("skip"))
    await handle_event(session, text("same"))
    await handle_event(session, reply(bot.row_id("slot:")))
    (first,) = await _orders(session)
    assert first.loyalty_points == 200
    return customer


async def test_points_stay_on_the_reprice_when_the_rules_allow_both(session, outlet, menu, bot):
    from tests.services.test_loyalty import balance

    await _promo(session, "SAVE5")
    customer = await _with_points(session, bot, stack=True)

    await handle_event(session, text("SAVE5"))

    old, new = await _orders(session)
    assert old.stage == OrderStage.CANCELLED and old.loyalty_state == "released"
    assert new.loyalty_points == 200 and new.promo_code == "SAVE5"
    assert await balance(session, customer) == 800


async def test_a_coupon_replaces_points_when_the_rules_say_one_or_the_other(
        session, outlet, menu, bot):
    from tests.services.test_loyalty import balance

    await _promo(session, "SAVE5")
    customer = await _with_points(session, bot, stack=False)

    await handle_event(session, text("SAVE5"))

    assert any("your 200 points stay in your balance" in m.body for m in bot.sent)
    old, new = await _orders(session)
    assert old.loyalty_state == "released"
    assert new.loyalty_points == 0 and new.promo_code == "SAVE5"
    assert "*Total: $27.19*" in bot.last().body
    assert await balance(session, customer) == 1000


async def test_before_the_summary_the_latest_choice_wins_and_is_explained(
        session, outlet, menu, bot):
    from tests.services.test_loyalty import configure, grant

    await _promo(session, "SAVE5")
    await configure(session)                                   # no stacking
    await _sign_up(session, bot)
    customer, _ = await _state(session)
    await grant(session, customer)
    await _add_drumstick(session, bot)

    await handle_event(session, text("redeem 200"))
    await handle_event(session, text("apply SAVE5"))
    assert "your 200 points stay in your balance" in bot.last().body
    _, conversation = await _state(session)
    assert not conversation.get("loyalty_points")

    await handle_event(session, text("redeem 200"))
    assert bot.last().body == p.COUPON_NOT_WITH_POINTS.format(code="SAVE5", points=200)
    _, conversation = await _state(session)
    assert not conversation.get("loyalty_points")


def test_the_summary_hint_fits_a_whatsapp_footer():
    assert len(p.SUMMARY_COUPON_HINT) <= 60
