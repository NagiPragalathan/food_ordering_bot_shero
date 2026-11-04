"""The welcome menu: when each number last got it, and the Gallabox flow
they picked from it

Revision ID: 0019_welcome_menu
Revises: 0018_nudge_rules
Create Date: 2026-11-04
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_welcome_menu"
down_revision = "0018_nudge_rules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "welcome_menu_chats",
        sa.Column("whatsapp_number", sa.String(length=20), nullable=False),
        sa.Column("menu_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gallabox_flow", sa.String(length=40), nullable=True),
        sa.Column("gallabox_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.PrimaryKeyConstraint("whatsapp_number"),
    )


def downgrade() -> None:
    op.drop_table("welcome_menu_chats")
