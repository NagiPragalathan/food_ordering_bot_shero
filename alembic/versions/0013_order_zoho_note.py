"""Orders noted on the customer's Zoho record when the Orders module is missing

Revision ID: 0013_order_zoho_note
Revises: 0012_nudges
Create Date: 2026-10-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_order_zoho_note"
down_revision = "0012_nudges"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("zoho_note", sa.String(80), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "zoho_note")
