"""A customer's saved delivery addresses - Home, Office, and so on.

One customer, many addresses. The one used for the latest order is copied
onto the `Customer` row as well, because the WhatsApp flow, the CRM push and
the delivery quote all read the address from there.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey


class CustomerAddress(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "customer_addresses"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Free text so a customer can say "Mum's place", with Home / Office /
    # Other offered as one-tap choices on the page.
    label: Mapped[str] = mapped_column(String(40), nullable=False)

    address_line1: Mapped[str] = mapped_column(String(255), nullable=False)
    apartment_unit: Mapped[str | None] = mapped_column(String(120))
    postal_code: Mapped[str] = mapped_column(String(20), nullable=False)
    delivery_instructions: Mapped[str | None] = mapped_column(String(500))

    # From the map pin or the phone's own location. Optional: a typed address
    # with only a ZIP still works, it is just less exact.
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)

    # The one pre-selected next time - the last address actually ordered to.
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CustomerAddress {self.label} {self.postal_code}>"
