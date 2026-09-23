"""Outlets (restaurants) and their delivery catchment."""

from __future__ import annotations

from sqlalchemy import Boolean, Float, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey


class Outlet(Base, UUIDPrimaryKey, Timestamps):
    """A Shero kitchen.

    `latitude`/`longitude` drive the nearby + serviceability calculation, and
    the pickup address block is what gets sent to Uber Direct for the quote.
    """

    __tablename__ = "outlets"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)

    # Pickup address - required by the Uber Direct quote call.
    address_line1: Mapped[str] = mapped_column(String(255), nullable=False)
    address_line2: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str] = mapped_column(String(120), nullable=False)
    state: Mapped[str] = mapped_column(String(80), nullable=False)
    postal_code: Mapped[str] = mapped_column(String(20), nullable=False)
    country: Mapped[str] = mapped_column(String(2), default="US", nullable=False)

    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)

    # Serviceability (spec step 10). Two modes, set per kitchen:
    #   "radius" - deliverable within delivery_radius_km
    #   "zips"   - deliverable only to the postal codes in service_zips
    #   "both"   - inside the radius AND in the ZIP list
    service_area_mode: Mapped[str] = mapped_column(
        String(10), default="radius", nullable=False
    )
    delivery_radius_km: Mapped[float] = mapped_column(Float, default=10.0, nullable=False)
    # Postal codes served, uppercased and stripped of spaces, e.g. ["21075"].
    service_zips: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    # The single active kitchen is the default for every order. Kept as a flag
    # rather than assuming one row, so a second kitchen can be added later
    # without a migration.
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    phone: Mapped[str | None] = mapped_column(String(32))
    # Notification target for the kitchen alert (spec step 17).
    kitchen_whatsapp: Mapped[str | None] = mapped_column(String(32))
    kitchen_email: Mapped[str | None] = mapped_column(String(255))

    # Cuisines this outlet serves, e.g. ["andhra", "kerala"].
    cuisines: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    # {"mon": [["11:00","22:00"]], ...} - slots are only generated inside these
    # windows (spec step 13: "only slots within operating hours are returned").
    operating_hours: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York",
                                          nullable=False)

    # How many orders one slot can absorb, used when auto-generating slots.
    slot_capacity: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    slot_length_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Outlet {self.code} {self.name}>"

    def serves_postal_code(self, postal_code: str | None) -> bool:
        """Is this postal code in the kitchen's ZIP list?"""
        if not self.service_zips:
            return True  # no list configured means no ZIP restriction
        target = _normalise_zip(postal_code)
        return bool(target) and target in {
            _normalise_zip(z) for z in self.service_zips
        }

    @property
    def full_address(self) -> str:
        parts = [self.address_line1, self.address_line2, self.city,
                 f"{self.state} {self.postal_code}"]
        return ", ".join(p for p in parts if p)


def _normalise_zip(value: str | None) -> str:
    """Uppercase, strip spaces and dashes, so '21075' == ' 21075 '."""
    return "".join((value or "").upper().split()).replace("-", "")
