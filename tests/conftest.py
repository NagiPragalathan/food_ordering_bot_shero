"""Shared pytest fixtures.

Database-backed tests run on in-memory SQLite for speed and zero setup. The
JSONB compilation shim that allows it lives in `app.db.types` and is
registered by importing the models, so tests and a local SQLite run of the app
behave identically.

Anything that depends on genuine Postgres behaviour (the conditional UPDATE in
slot holding, concurrent capacity claims) should be exercised against a real
Postgres in CI - see docs/testing.md.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.models import Base, Category, Cuisine, Customer, MenuItem, Outlet


@pytest.fixture(autouse=True)
def _answer_everyone(monkeypatch):
    """The suite runs as production: the bot answers every number.

    A developer's `.env` may restrict BOT_ALLOWED_NUMBERS to their own phone
    for live testing on the business number. That is a property of one
    machine and must not decide whether the tests pass. A test that wants
    test mode sets it itself, which overrides this.
    """
    monkeypatch.setattr(settings, "bot_allowed_numbers", "")
    monkeypatch.setattr(settings, "bot_reply_mode", "")
    monkeypatch.setattr(settings, "bot_reply_trigger", "")
    monkeypatch.setattr(settings, "bot_trigger_keywords", "")
    # The bot's own channel comes from .env; a test that checks the channel
    # filter sets it itself (tests/integrations/test_channel_filter.py).
    monkeypatch.setattr(settings, "gallabox_channel_id", "")
    monkeypatch.setattr(settings, "whatsapp_business_number", "")


@pytest.fixture(autouse=True)
def _templates_approved(monkeypatch):
    """Every template counts as approved unless a test says otherwise.

    The real check asks Gallabox; a test must never reach the network. Tests
    of the not-yet-approved fallback patch this to return False.
    """
    from app.integrations.gallabox import template_status

    async def approved(name: str) -> bool:
        return True

    monkeypatch.setattr(template_status, "is_approved", approved)


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    """A fresh empty database per test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as db_session:
        yield db_session

    await engine.dispose()


@pytest_asyncio.fixture
async def outlet(session: AsyncSession) -> Outlet:
    """An Edison outlet open 11:00-21:00 daily, 12 km delivery radius."""
    record = Outlet(
        code="EDISON",
        name="Shero Edison",
        address_line1="123 Oak Tree Road",
        city="Edison",
        state="NJ",
        postal_code="08820",
        country="US",
        latitude=40.5187,
        longitude=-74.4121,
        delivery_radius_km=12.0,
        cuisines=["andhra", "kerala"],
        timezone="America/New_York",
        slot_capacity=2,
        slot_length_minutes=60,
        is_primary=True,
        service_area_mode="radius",
        operating_hours={
            day: [["11:00", "21:00"]]
            for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        },
    )
    session.add(record)
    await session.flush()
    return record


@pytest_asyncio.fixture
async def menu(session: AsyncSession):
    """A small menu: two cuisines, three categories, four dishes.

    Mirrors the shape of the client's sheet (MRP as the customer price, PPP as
    cost) without pulling in all 287 rows.
    """
    from decimal import Decimal

    built: dict[str, MenuItem] = {}
    spec = [
        ("Chettinad", "chettinad", [
            ("Sambar", "sambar", [
                ("Drumstick Sambar", "12.50", "8.16", "16 oz pack", "Serves 3 - 4"),
                ("Beans Sambar", "11.28", "7.36", "16 oz pack", "Serves 3 - 4"),
            ]),
            ("Rasam", "rasam", [
                ("Tomato Rasam", "9.50", "6.20", "16 oz pack", "Serves 3 - 4"),
            ]),
        ]),
        ("Kerala", "kerala", [
            ("Sambar", "sambar", [
                ("Appam", "3.25", "2.12", "6 pieces", "Serves 2"),
            ]),
        ]),
    ]

    for c_index, (c_name, c_slug, categories) in enumerate(spec):
        cuisine = Cuisine(name=c_name, slug=c_slug, position=c_index, is_active=True)
        session.add(cuisine)
        await session.flush()

        for cat_index, (cat_name, cat_slug, items) in enumerate(categories):
            category = Category(cuisine_id=cuisine.id, name=cat_name,
                                slug=cat_slug, position=cat_index, is_active=True)
            session.add(category)
            await session.flush()

            for i_index, (name, price, cost, pack, serves) in enumerate(items):
                retailer_id = f"{c_slug}-{cat_slug}-{name.lower().replace(' ', '-')}"
                item = MenuItem(
                    category_id=category.id, retailer_id=retailer_id, name=name,
                    description=f"{pack}. {serves}.", price=Decimal(price),
                    cost_price=Decimal(cost), pack_size=pack, serves=serves,
                    position=i_index, is_available=True,
                )
                session.add(item)
                built[retailer_id] = item

    await session.flush()
    return built


@pytest_asyncio.fixture
async def far_outlet(session: AsyncSession) -> Outlet:
    """Jersey City: ~38 km from Edison, so outside a 10 km radius."""
    record = Outlet(
        code="JERSEYCITY",
        name="Shero Jersey City",
        address_line1="45 Newark Avenue",
        city="Jersey City",
        state="NJ",
        postal_code="07302",
        country="US",
        latitude=40.7215,
        longitude=-74.0466,
        delivery_radius_km=10.0,
        cuisines=["kerala"],
        timezone="America/New_York",
        operating_hours={"mon": [["12:00", "21:00"]]},
    )
    session.add(record)
    await session.flush()
    return record


@pytest_asyncio.fixture
async def customer(session: AsyncSession) -> Customer:
    record = Customer(
        whatsapp_number="17325550142",
        name="Asha Menon",
        email="asha@example.com",
        address_line1="12 Maple Street",
        contact_number="17325550142",
        postal_code="08820",
        latitude=40.5200,
        longitude=-74.4100,
    )
    session.add(record)
    await session.flush()
    return record
