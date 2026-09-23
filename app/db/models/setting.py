"""Settings editable from the admin dashboard, and admin users.

Credentials live in two places by design:

  * `.env`  - the floor. Always present, needed to boot.
  * this table - overrides saved through the admin Settings page, so the
    client can rotate a Zoho secret or point at a new WhatsApp channel
    without a redeploy.

DB values win over environment values. Secrets are encrypted at rest with
Fernet (see `app/services/settings_store.py`), so a database dump does not
hand over the Stripe key.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey


class AppSetting(Base, UUIDPrimaryKey, Timestamps):
    """One configuration value, keyed by its environment variable name."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    # Encrypted when is_secret, plaintext otherwise.
    value: Mapped[str | None] = mapped_column(Text)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String(120))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AppSetting {self.key} secret={self.is_secret}>"


class AdminUser(Base, UUIDPrimaryKey, Timestamps):
    """Someone who can sign in to the dashboard.

    Passwords are Argon2 hashes - never reversible, never logged.
    """

    __tablename__ = "admin_users"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True,
                                       nullable=False)
    name: Mapped[str | None] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AdminUser {self.email}>"
