"""Mark ready and its Shiprocket rider

Revision ID: 0024_shiprocket_ready
Revises: 0023_crm_outbox
Create Date: 2026-11-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0024_shiprocket_ready"
down_revision = "0023_crm_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outlets", sa.Column("shiprocket_pickup", sa.String(length=36), nullable=True))
    op.add_column("orders", sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column(
        "courier", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
        server_default=sa.text("'{}'")))


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.drop_column("courier")
        batch.drop_column("ready_at")
    with op.batch_alter_table("outlets") as batch:
        batch.drop_column("shiprocket_pickup")
