"""Saved delivery addresses (Home, Office, ...) behind the web ordering page.

The rule that matters most: an address id is only ever looked up among the
link owner's own addresses, so one customer's page cannot read, edit, delete
or order to another customer's address.
"""

from __future__ import annotations

import pytest

from app.db.models import Customer
from app.services import addresses
from app.services.addresses import AddressError

HOME = {"label": "Home", "address_line1": "12 Maple Street", "postal_code": "08820",
        "latitude": 40.52, "longitude": -74.41}
OFFICE = {"label": "Office", "address_line1": "1 Wood Ave", "apartment_unit": "Suite 4",
          "postal_code": "08830", "delivery_instructions": "Front desk"}


async def _stranger(session) -> Customer:
    other = Customer(whatsapp_number="14155550199", name="Someone Else")
    session.add(other)
    await session.flush()
    return other


# --- validation ------------------------------------------------------------------
def test_street_and_zip_are_required():
    with pytest.raises(AddressError, match="street"):
        addresses.clean({"postal_code": "08820"})
    with pytest.raises(AddressError, match="ZIP"):
        addresses.clean({"address_line1": "12 Maple Street"})


def test_text_is_trimmed_and_a_missing_label_becomes_other():
    fields = addresses.clean({"address_line1": "  12   Maple  Street ", "postal_code": " 08820 "})
    assert fields["address_line1"] == "12 Maple Street"
    assert fields["postal_code"] == "08820"
    assert fields["label"] == "Other"
    assert fields["apartment_unit"] is None


def test_a_half_pin_or_an_impossible_one_is_dropped():
    """Without both coordinates the ZIP is used instead - never half a point."""
    half = addresses.clean({**HOME, "longitude": None})
    assert half["latitude"] is None and half["longitude"] is None
    wild = addresses.clean({**HOME, "latitude": 123.0})
    assert wild["latitude"] is None and wild["longitude"] is None
    junk = addresses.clean({**HOME, "latitude": "north"})
    assert junk["latitude"] is None


def test_a_custom_label_is_kept():
    assert addresses.clean({**HOME, "label": "Mum's place"})["label"] == "Mum's place"


# --- saving and listing ------------------------------------------------------------
async def test_the_first_address_is_the_default_and_later_ones_are_not(session, customer):
    customer.address_line1 = None          # no carried-over address in the way
    home = await addresses.save(session, customer, HOME)
    office = await addresses.save(session, customer, OFFICE)

    assert home.is_default is True
    assert office.is_default is False
    listed = await addresses.list_for(session, customer)
    assert [a.label for a in listed] == ["Home", "Office"]


async def test_editing_changes_the_same_row(session, customer):
    customer.address_line1 = None
    home = await addresses.save(session, customer, HOME)
    edited = await addresses.save(session, customer, {**HOME, "apartment_unit": "2B"},
                                  address_id=str(home.id))

    assert edited.id == home.id
    assert edited.apartment_unit == "2B"
    assert len(await addresses.list_for(session, customer)) == 1


async def test_an_existing_single_address_is_carried_over_as_home(session, customer):
    """Customers who ordered before this change must not retype their address."""
    listed = await addresses.list_for(session, customer)

    assert len(listed) == 1
    assert listed[0].label == "Home"
    assert listed[0].address_line1 == "12 Maple Street"
    assert listed[0].is_default is True
    # And only once.
    assert len(await addresses.list_for(session, customer)) == 1


async def test_there_is_a_cap_on_saved_addresses(session, customer, monkeypatch):
    customer.address_line1 = None
    monkeypatch.setattr(addresses, "MAX_ADDRESSES", 2)
    await addresses.save(session, customer, HOME)
    await addresses.save(session, customer, OFFICE)
    with pytest.raises(AddressError, match="up to 2"):
        await addresses.save(session, customer, HOME)


# --- ownership -------------------------------------------------------------------
async def test_another_customers_address_cannot_be_read_edited_or_deleted(session, customer):
    stranger = await _stranger(session)
    theirs = await addresses.save(session, stranger, HOME)

    with pytest.raises(AddressError, match="could not be found"):
        await addresses.get_owned(session, customer, theirs.id)
    with pytest.raises(AddressError, match="could not be found"):
        await addresses.save(session, customer, OFFICE, address_id=str(theirs.id))
    with pytest.raises(AddressError, match="could not be found"):
        await addresses.delete(session, customer, theirs.id)

    assert (await addresses.get_owned(session, stranger, theirs.id)).address_line1 == \
        "12 Maple Street"


async def test_a_garbage_id_is_a_friendly_error_not_a_crash(session, customer):
    with pytest.raises(AddressError, match="Choose a delivery address"):
        await addresses.get_owned(session, customer, "not-a-uuid")
    with pytest.raises(AddressError):
        await addresses.get_owned(session, customer, None)


# --- deleting and ordering ---------------------------------------------------------
async def test_deleting_the_default_hands_the_default_to_another(session, customer):
    customer.address_line1 = None
    home = await addresses.save(session, customer, HOME)
    office = await addresses.save(session, customer, OFFICE)

    await addresses.delete(session, customer, home.id)

    listed = await addresses.list_for(session, customer)
    assert [a.id for a in listed] == [office.id]
    assert listed[0].is_default is True


async def test_ordering_to_an_address_copies_it_and_makes_it_the_default(session, customer):
    customer.address_line1 = None
    customer.contact_number = "19999999999"         # typed in the old form
    await addresses.save(session, customer, HOME)
    office = await addresses.save(session, customer, OFFICE)

    await addresses.use_for_order(session, customer, office)

    assert customer.address_line1 == "1 Wood Ave"
    assert customer.apartment_unit == "Suite 4"
    assert customer.postal_code == "08830"
    assert customer.delivery_instructions == "Front desk"
    # The driver calls the WhatsApp number - whatever was typed before.
    assert customer.contact_number == customer.whatsapp_number
    defaults = [a.label for a in await addresses.list_for(session, customer) if a.is_default]
    assert defaults == ["Office"]


async def test_ordering_to_a_pinned_address_moves_the_customers_point(session, customer):
    customer.address_line1 = None
    home = await addresses.save(session, customer, HOME)
    await addresses.use_for_order(session, customer, home)
    assert (customer.latitude, customer.longitude) == (40.52, -74.41)
