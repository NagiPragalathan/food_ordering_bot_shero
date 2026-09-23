"""Database fixtures work end to end (proves the SQLite/JSONB shim)."""

from sqlalchemy import select

from app.db.models import Customer, Outlet


async def test_outlet_fixture_persists_json_columns(session, outlet):
    result = await session.execute(select(Outlet).where(Outlet.code == "EDISON"))
    found = result.scalar_one()
    assert found.cuisines == ["andhra", "kerala"]
    assert found.operating_hours["mon"] == [["11:00", "21:00"]]
    assert found.full_address.startswith("123 Oak Tree Road")


async def test_customer_fixture(session, customer):
    result = await session.execute(select(Customer))
    found = result.scalar_one()
    assert found.has_details is True
    assert found.whatsapp_number == "17325550142"
