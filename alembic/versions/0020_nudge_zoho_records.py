"""Each nudge's record in Zoho's Nudge Messages module

Revision ID: 0020_nudge_zoho_records
Revises: 0019_welcome_menu
Create Date: 2026-11-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_nudge_zoho_records"
down_revision = "0019_welcome_menu"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("nudge_sends") as batch:
        batch.add_column(sa.Column("zoho_id", sa.String(length=30), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("nudge_sends") as batch:
        batch.drop_column("zoho_id")
