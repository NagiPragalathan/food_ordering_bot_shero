"""Customers - one row per WhatsApp number, mirrored into Zoho.

Returning customers are matched by WhatsApp number so no duplicate Lead is
ever created (spec section 2).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey
from app.db.models.enums import LeadStage


class Customer(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "customers"

    whatsapp_number: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False
    )
    name: Mapped[str | None] = mapped_column(String(160))
    # The name on their WhatsApp profile, as WhatsApp sends it. Kept apart
    # from `name` (what they typed): profile names are often nicknames.
    whatsapp_profile_name: Mapped[str | None] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(255))

    # Delivery details (spec step 12).
    address_line1: Mapped[str | None] = mapped_column(String(255))
    apartment_unit: Mapped[str | None] = mapped_column(String(120))
    delivery_instructions: Mapped[str | None] = mapped_column(String(500))
    contact_number: Mapped[str | None] = mapped_column(String(32))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)

    cuisine_preference: Mapped[str | None] = mapped_column(String(60))
    preferred_outlet_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outlets.id", ondelete="SET NULL")
    )
    distance_km: Mapped[float | None] = mapped_column(Float)

    # Attribution from the Click-to-WhatsApp ad (spec step 1).
    lead_source: Mapped[str] = mapped_column(String(80), default="Meta Ad", nullable=False)
    ad_id: Mapped[str | None] = mapped_column(String(80))
    campaign_id: Mapped[str | None] = mapped_column(String(80))
    referral_payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # Zoho linkage (docs/zoho-setup.md): a Lead until the first payment, then
    # the Contact that Zoho's conversion made of it. Only one of the two is set.
    zoho_lead_id: Mapped[str | None] = mapped_column(String(40), index=True)
    zoho_contact_id: Mapped[str | None] = mapped_column(String(40), index=True)
    lead_stage: Mapped[str] = mapped_column(
        String(40), default=LeadStage.NEW_ENQUIRY, nullable=False
    )
    # {"New Enquiry": "2026-09-21T10:00:00Z", ...} - the spec requires a
    # timestamp per stage change so reports show where customers drop off.
    stage_timestamps: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    is_converted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Last WhatsApp "we do not deliver to <address> yet" (services/out_of_area);
    # it goes at most once a day, however often they try another address.
    out_of_area_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Customer {self.whatsapp_number} stage={self.lead_stage}>"

    @property
    def has_details(self) -> bool:
        """True once name and email are captured (spec steps 3-4)."""
        return bool(self.name and self.email)

    @property
    def greeting_name(self) -> str:
        """What to put after "Hi" in a message.

        A lead we contacted first has no name yet, and a template renders a
        missing parameter as the empty string - "Hi , your order is ready".
        Every outgoing greeting goes through here so that cannot happen.
        """
        return (self.name or "").strip() or "there"
