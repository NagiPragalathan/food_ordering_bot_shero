"""The welcome menu, per WhatsApp number (services/welcome_menu.py).

One row per number the menu has been sent to or that picked a Gallabox flow
from it. Keyed by the number, not a customer: the menu goes before anyone
has a customer record, and a Party Order enquiry never gets one.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps


class WelcomeMenuChat(Base, Timestamps):
    __tablename__ = "welcome_menu_chats"

    whatsapp_number: Mapped[str] = mapped_column(String(20), primary_key=True)
    # When the menu last went: once a day at most.
    menu_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The Gallabox flow they picked (the button's label) and when: the bot
    # stays out of the chat while that flow runs.
    gallabox_flow: Mapped[str | None] = mapped_column(String(40))
    gallabox_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
