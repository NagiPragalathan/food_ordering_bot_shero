"""The ordering bot's welcome menu can send a button to Shero Food Training,
and neither bot steps into the other's chat (services/welcome_menu.py,
app/sft/router.py).

What went wrong: a tester part-way through SFT got the food menu (Party
Order / Sweet & Snacks / Single Meal Order) in the middle of the course
journey. Now the menu can carry SFT itself, a tap on it is SFT's whatever
its label says, and nobody SFT is talking to gets the food menu.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core import tenants
from app.core.config import settings
from app.services import reply_triggers as rt
from app.services import welcome_menu as wm
from app.sft import config, leads, router
from app.sft import content as c
from tests.sft.conftest import NUMBER, event

COURSE = "Cooking Course"            # no SFT keyword in it: the button alone makes it SFT's


@pytest.fixture
def menu_with_sft(monkeypatch):
    monkeypatch.setattr(settings, "bot_reply_trigger", rt.KEYWORDS)
    monkeypatch.setattr(settings, "bot_trigger_keywords",
                        rt.dump([rt.Trigger("ORDER FOOD", rt.CONTAINS, rt.WHATSAPP)]))
    menu = wm.Menu(enabled=True, buttons=(
        wm.MenuButton("Party Order", wm.GALLABOX), wm.MenuButton("Sweet & Snacks", wm.GALLABOX),
        wm.MenuButton("Single Meal Order", rt.WHATSAPP), wm.MenuButton(COURSE, wm.SFT)))
    monkeypatch.setattr(settings, "bot_welcome_menu", wm.dump(menu))
    return menu


def test_sft_is_a_place_a_button_can_go_on_shero_us_only():
    assert wm.SFT in wm.goes_to_options()
    with tenants.use(tenants.DST):
        assert wm.SFT not in wm.goes_to_options()
    twice = wm.Menu(buttons=(wm.MenuButton("SFT", wm.SFT), wm.MenuButton("Course", wm.SFT)))
    assert "Only one button can go to Shero Food Training." in wm.problems(twice)


async def test_four_options_go_as_a_list(session, cfg, menu_with_sft, rec):
    assert wm.parse(wm.dump(menu_with_sft)) == menu_with_sft
    assert await wm.send(session, NUMBER)
    kind, body, rows = rec.last()
    assert kind == "list" and body.startswith("Hi!")
    assert [title for _, title in rows] == ["Party Order", "Sweet & Snacks", "Single Meal Order",
                                            COURSE]


async def test_three_options_stay_buttons(session, rec, monkeypatch):
    monkeypatch.setattr(settings, "bot_welcome_menu", wm.dump(wm.Menu(enabled=True)))
    assert await wm.send(session, NUMBER)
    assert rec.last()[0] == "buttons"


async def test_the_sft_button_starts_the_course_journey(session, cfg, rec, menu_with_sft):
    taken = await router.route(session, event(button=COURSE, title=COURSE))
    assert taken and taken["status"] == "ok"
    lead = await leads.get(session, NUMBER)
    assert lead is not None and lead.source_detail == "welcome menu"
    assert rec.last()[1] == c.say(cfg, "ask_name")
    # Typed rather than tapped: the same.
    rec.clear()
    assert await router.route(session, event(COURSE, number="15551230009"))
    assert rec.last()[1] == c.say(cfg, "ask_name")


async def test_the_sft_button_restarts_a_lead_part_way(session, cfg, rec, menu_with_sft):
    await router.route(session, event("SFT"))
    await router.route(session, event("Priya"))
    rec.clear()
    await router.route(session, event(button=COURSE, title=COURSE))
    assert rec.buttons() == [b.title for b in c.GOAL_BUTTONS]       # the welcome again


async def test_with_sft_off_its_button_is_nobodys(session, rec, menu_with_sft):
    await config.save(session, {"mode": "off"}, "t")
    assert await router.route(session, event(button=COURSE, title=COURSE)) is None
    decision = await rt.decide(session, event(button=COURSE, title=COURSE))
    assert not decision.answer and decision.action == ""            # not ordering, no menu


async def test_nobody_mid_sft_gets_the_food_menu(session, cfg, rec, menu_with_sft):
    await router.route(session, event("SFT"))
    await router.route(session, event("Priya"))
    lead = await leads.get(session, NUMBER)
    # "hello" isn't SFT's (no keyword, and say the lead went quiet on a step
    # that takes no typing) - and still no food menu while SFT has the chat.
    lead.step = c.IDLE
    decision = await rt.decide(session, event("hello"))
    assert decision.action != wm.SEND and not decision.answer
    # Food is still one keyword away.
    assert (await rt.decide(session, event("order food"))).answer
    # A day after SFT's last word, the chat is new again: the menu.
    later = leads.now() + timedelta(hours=25)
    assert (await rt.decide(session, event("hello"), later)).action == wm.SEND


async def test_the_food_menu_still_goes_to_new_chats(session, menu_with_sft):
    assert (await rt.decide(session, event("hello", number="15550001111"))).action == wm.SEND


async def test_in_test_mode_only_testers_see_the_sft_option(session, rec, menu_with_sft):
    await config.save(session, {"mode": "test", "test_numbers": NUMBER}, "t")
    assert await wm.send(session, "15550002222")                    # a customer
    kind, _, buttons = rec.last()
    assert kind == "buttons" and [t for _, t in buttons] == [
        "Party Order", "Sweet & Snacks", "Single Meal Order"]       # the menu as before
    assert await wm.send(session, NUMBER)                           # the tester
    kind, _, rows = rec.last()
    assert kind == "list" and [t for _, t in rows][-1] == COURSE


async def test_with_sft_off_nobody_sees_the_sft_option(session, rec, menu_with_sft):
    await config.save(session, {"mode": "off"}, "t")
    assert await wm.send(session, NUMBER)
    assert rec.last()[0] == "buttons" and COURSE not in [t for _, t in rec.last()[2]]
