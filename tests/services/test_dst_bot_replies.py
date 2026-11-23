"""DST India's WhatsApp number: routed to DST India by its channel, answering
nobody until switched on, then starting the ordering bot only on "Hungry
Now? Order Food" (or Order Food) while its Gallabox bot flow keeps Party
Order, Food Subscription and Sweets & Snacks. Shero US is unchanged."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core import tenants
from app.core.config import settings, settings_for
from app.db.models import AdminUser, Base, Customer, WelcomeMenuChat
from app.schemas.inbound import parse_inbound
from app.services import allowlist, reply_triggers as rt, welcome_menu as wm

US_CHANNEL, US_NUMBER = "6a2fee09a806dc48e9e4eef7", "14438011011"
DST_CHANNEL, DST_NUMBER = "6af30a65a7aa4cd16b22569a", "919360056005"
CUSTOMER = "919840012345"


def _body(channel_id: str, channel_number: str, text: str = "hi", *,
          button: str | None = None) -> dict:
    whatsapp = {"from": CUSTOMER, "type": "text", "text": {"body": text}}
    if button:
        whatsapp = {"from": CUSTOMER, "type": "interactive",
                    "interactive": {"type": "button_reply",
                                    "button_reply": {"id": button, "title": button}}}
    return {"accountId": "acc", "channelType": "whatsapp", "id": f"m-{text}-{button}",
            "channelId": channel_id, "channelNumber": channel_number,
            "contact": {"name": "Asha"}, "whatsapp": whatsapp}


@pytest.fixture
def channels(monkeypatch):
    us, dst = settings_for(tenants.US), settings_for(tenants.DST)
    monkeypatch.setattr(us, "gallabox_channel_id", US_CHANNEL)
    monkeypatch.setattr(us, "whatsapp_business_number", US_NUMBER)
    monkeypatch.setattr(dst, "gallabox_channel_id", DST_CHANNEL)
    monkeypatch.setattr(dst, "whatsapp_business_number", DST_NUMBER)
    return dst


@pytest_asyncio.fixture
async def dst_db(monkeypatch):
    """DST India's own database: a second in-memory one, used by the webhook
    when it hands a DST message over."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as own:
        from app.api.routes import webhooks_gallabox

        @asynccontextmanager
        async def scope():
            assert tenants.current() == tenants.DST
            yield own
            await own.flush()

        monkeypatch.setattr(webhooks_gallabox, "session_scope", scope)
        yield own
    await engine.dispose()


async def _webhook(session, monkeypatch, body: dict) -> tuple[dict, list]:
    from app.api.routes import webhooks_gallabox

    handled = []

    async def spy(own_session, event, **kwargs):
        handled.append((tenants.current().key, settings.currency, own_session, event,
                        kwargs.get("reply")))
        return True

    monkeypatch.setattr(webhooks_gallabox, "handle_event", spy)
    monkeypatch.setattr(webhooks_gallabox, "verify_gallabox_token", lambda t: True)

    class FakeRequest:
        headers: dict = {}

        async def json(self):
            return body

    result = await webhooks_gallabox.gallabox_webhook(
        FakeRequest(), session=session, authorization="t",
        x_gallabox_token=None, x_webhook_secret=None, token=None)
    return result, handled


# --- routing ---------------------------------------------------------------------------
def test_a_dst_channel_message_belongs_to_dst_india(channels):
    from app.integrations.gallabox import channel

    assert channel.tenant_for(parse_inbound(_body(DST_CHANNEL, DST_NUMBER))) == tenants.DST
    assert channel.tenant_for(parse_inbound(_body(US_CHANNEL, US_NUMBER))) == tenants.US
    assert channel.tenant_for(parse_inbound(_body("659f88031fb2db4974d16ee8", "1"))) is None


async def test_dst_answers_nobody_until_switched_on(session, dst_db, channels, monkeypatch):
    result, handled = await _webhook(session, monkeypatch,
                                     _body(DST_CHANNEL, DST_NUMBER, "Hungry Now? Order Food"))
    assert result == {"status": "ignored", "reason": "sender not whitelisted"}
    assert handled == []


@pytest.fixture
def dst_live(channels, monkeypatch):
    monkeypatch.setattr(channels, "bot_reply_mode", "all")
    return channels


async def test_hungry_now_starts_the_bot_in_dst_india(session, dst_db, dst_live, monkeypatch):
    result, handled = await _webhook(
        session, monkeypatch, _body(DST_CHANNEL, DST_NUMBER, button="Hungry Now? Order Food"))
    assert result["status"] == "ok"
    [(business, currency, used, event, reply)] = handled
    # DST India's settings and its own database, not Shero US's.
    assert (business, currency, reply) == ("dst", "INR", rt.WHATSAPP)
    assert used is dst_db and used is not session


async def test_order_food_typed_starts_it_too(session, dst_db, dst_live, monkeypatch):
    result, handled = await _webhook(session, monkeypatch,
                                     _body(DST_CHANNEL, DST_NUMBER, "I want to order food"))
    assert result["status"] == "ok" and handled[0][0] == "dst"


async def test_the_gallabox_flow_keeps_its_other_options(session, dst_db, dst_live, monkeypatch):
    for option in ("Party Order", "Food Subscription", "Sweets & Snacks"):
        result, handled = await _webhook(session, monkeypatch,
                                         _body(DST_CHANNEL, DST_NUMBER, button=option))
        assert result == {"status": "ignored", "reason": "Gallabox flow"} and handled == []
    # While that flow runs, its own buttons are its own.
    result, handled = await _webhook(session, monkeypatch,
                                     _body(DST_CHANNEL, DST_NUMBER, button="Birthday"))
    assert result["status"] == "ignored" and handled == []
    assert (await dst_db.get(WelcomeMenuChat, CUSTOMER)).gallabox_flow == "Sweets & Snacks"


async def test_a_hello_is_left_to_the_gallabox_flow(session, dst_db, dst_live, monkeypatch):
    """The flow sends the Welcome Menu: the bot never sends one of its own."""
    sent = []

    async def send(*_, **__):
        sent.append(1)
        return True

    monkeypatch.setattr(wm, "send", send)
    result, handled = await _webhook(session, monkeypatch, _body(DST_CHANNEL, DST_NUMBER, "Hi"))
    assert result == {"status": "ignored", "reason": "no trigger keyword"}
    assert handled == [] and sent == []
    # Nothing recorded for Shero US either.
    from sqlalchemy import func, select
    assert (await session.execute(select(func.count(Customer.id)))).scalar_one() == 0


async def test_us_messages_still_go_to_shero_us(session, dst_db, channels, monkeypatch):
    result, handled = await _webhook(session, monkeypatch, _body(US_CHANNEL, US_NUMBER, "hi"))
    assert result["status"] == "ok"
    [(business, currency, used, _, _)] = handled
    assert (business, currency) == ("us", "USD") and used is session


# --- the defaults behind it --------------------------------------------------------------
def test_dst_defaults(channels):
    with tenants.use(tenants.DST):
        assert allowlist.mode() == allowlist.OFF and allowlist.is_off()
        assert not allowlist.permits(CUSTOMER) and allowlist.is_restricted()
        assert rt.mode() == rt.KEYWORDS
        assert [t.keyword for t in rt.triggers()] == ["Hungry Now? Order Food", "Order Food"]
        menu = wm.load()
        assert menu.enabled and not menu.we_send and menu.sent_by == wm.GALLABOX_SENDS
        assert [b.label for b in menu.buttons] == ["Party Order", "Food Subscription",
                                                   "Sweets & Snacks", "Hungry Now? Order Food"]
        assert menu.button("Hungry Now? Order Food").goes_to == rt.WHATSAPP
        assert wm.problems(menu) == []           # 4 options, 22 characters: fine for a list


def test_us_defaults_are_unchanged():
    assert allowlist.mode() == allowlist.ALL and rt.mode() == rt.ANY and rt.triggers() == []
    menu = wm.load()
    assert not menu.enabled and menu.we_send and menu.buttons == wm.DEFAULT_BUTTONS
    assert wm.dump(menu).find("sent_by") == -1


def test_a_menu_the_bot_sends_keeps_whatsapp_s_limits():
    # Four or more options go as a list (24 characters each); three or fewer
    # are buttons (20 each); never more than ten.
    menu = wm.Menu(enabled=True, buttons=tuple(wm.MenuButton(f"Option {i}", wm.GALLABOX)
                                               for i in range(4)))
    assert wm.problems(menu) == [] and menu.as_list
    eleven = wm.Menu(enabled=True, buttons=tuple(wm.MenuButton(f"Option {i}", wm.GALLABOX)
                                                 for i in range(11)))
    assert "At most 10 options." in wm.problems(eleven)
    long = "Twenty-two characters"
    three = wm.Menu(enabled=True, buttons=(wm.MenuButton(long, wm.GALLABOX),))
    assert any("longer than 20" in p for p in wm.problems(three))
    listed = wm.Menu(enabled=True, buttons=(wm.MenuButton(long, wm.GALLABOX),) + menu.buttons[:3])
    assert wm.problems(listed) == []
    by_gallabox = wm.Menu(enabled=True, buttons=menu.buttons, sent_by=wm.GALLABOX_SENDS)
    assert wm.problems(by_gallabox) == []
    assert wm.parse(wm.dump(by_gallabox)) == by_gallabox


async def test_the_bot_never_sends_a_gallabox_menu(session, monkeypatch):
    monkeypatch.setattr(settings, "bot_welcome_menu", wm.dump(wm.Menu(
        enabled=True, buttons=wm.DST_BUTTONS, sent_by=wm.GALLABOX_SENDS)))
    assert await wm.send(session, CUSTOMER) is False


# --- the Bot replies page ------------------------------------------------------------------
@pytest.fixture
def admin(session):
    from app.admin import router
    from app.admin.deps import require_admin
    from app.db.session import get_primary_session, get_session

    app = FastAPI()
    app.add_middleware(tenants.TenantPathMiddleware)
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_primary_session] = lambda: session
    app.dependency_overrides[require_admin] = lambda: AdminUser(
        email="ops@shero.test", name="Ops", password_hash="x", is_active=True)
    return TestClient(app, follow_redirects=False)


def test_the_dst_page_shows_the_bot_is_off(admin, channels):
    page = admin.get("/dst/admin/bot-replies")
    assert page.status_code == 200
    assert "Off: answering nobody on the DST India number" in page.text
    assert "Nobody (off)" in page.text and "Our Gallabox bot flow sends it" in page.text
    assert 'value="Hungry Now? Order Food"' in page.text


def test_the_us_page_is_as_before(admin):
    page = admin.get("/admin/bot-replies")
    assert page.status_code == 200
    assert "Live: replying to everyone" in page.text and "Nobody (off)" not in page.text


def test_dst_can_be_switched_to_test_numbers_and_back_off(admin, channels, session,
                                                         monkeypatch):
    # Saving applies the values to DST India's live settings: put them back after.
    from app.services import settings_store

    for key in ("bot_reply_mode", "bot_allowed_numbers"):
        monkeypatch.setattr(channels, key, getattr(channels, key))
    monkeypatch.setattr(settings_store, "_applied", settings_store.tenants.PerTenant(set))

    saved = admin.post("/dst/admin/bot-replies",
                       data={"mode": "allowlist", "number": ["919840012345"], "name": ["Asha"]})
    assert saved.status_code == 303
    with tenants.use(tenants.DST):
        assert allowlist.mode() == allowlist.ALLOWLIST and allowlist.permits(CUSTOMER)
    assert allowlist.mode() == allowlist.ALL          # Shero US untouched

    off = admin.post("/dst/admin/bot-replies", data={"mode": "off"})
    assert off.status_code == 303
    with tenants.use(tenants.DST):
        assert allowlist.is_off() and not allowlist.permits(CUSTOMER)
