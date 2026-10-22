"""Separate SQLite connections competing for one points wallet."""
import asyncio
import uuid
from decimal import Decimal
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.db.models import Base, Customer, LoyaltyEntry, Order, PaymentStatus
from app.services import loyalty

async def test_simultaneous_redemptions_cannot_overdraw(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "loyalty.db").as_posix())
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    customer_id = uuid.uuid4()
    async with factory() as session, session.begin():
        session.add(Customer(id=customer_id, whatsapp_number="15550001111"))
        await session.flush()
        await loyalty.adjust(session, customer_id, 200, note="Opening", actor="test", request_id=uuid.uuid4())
    async def reserve(number):
        try:
            async with factory() as session, session.begin():
                order = Order(order_number=number, customer_id=customer_id, loyalty_points=200, total=Decimal("10"))
                session.add(order)
                await session.flush()
                await loyalty.reserve(session, order)
            return "reserved"
        except loyalty.LoyaltyError:
            return "refused"
    results = await asyncio.gather(reserve("SHO-CONCURRENT-A"), reserve("SHO-CONCURRENT-B"))
    assert sorted(results) == ["refused", "reserved"]
    async with factory() as session:
        assert (await loyalty.account(session, customer_id)).balance == 0
        assert await session.scalar(select(func.count(LoyaltyEntry.id)).where(LoyaltyEntry.kind == "hold")) == 1
    await engine.dispose()

async def test_simultaneous_first_purchases_award_first_bonus_once(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "earn.db").as_posix())
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    customer_id, ids = uuid.uuid4(), [uuid.uuid4(), uuid.uuid4()]
    rules = loyalty.Policy(enabled=True).model_dump(mode="json")
    snapshot = {**rules, "eligible_subtotal":"20", "eligible_net_spend":"20", "eligible_kitchen":True}
    async with factory() as session, session.begin():
        session.add(Customer(id=customer_id, whatsapp_number="15550002222"))
        await session.flush()
        for i, order_id in enumerate(ids):
            session.add(Order(id=order_id,order_number=f"SHO-EARN-{i}",customer_id=customer_id,
                              total=Decimal("20"),payment_status=PaymentStatus.PAID,loyalty_policy=snapshot))
    async def earn(order_id):
        async with factory() as session, session.begin():
            order = await session.get(Order, order_id)
            return await loyalty.paid(session, order)
    values = await asyncio.gather(*(earn(order_id) for order_id in ids))
    assert sorted(values) == [20,45]
    async with factory() as session:
        wallet = await loyalty.account(session, customer_id)
        assert wallet.balance == 65 and wallet.eligible_orders == 2
    await engine.dispose()
