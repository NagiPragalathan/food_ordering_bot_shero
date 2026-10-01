"""The kitchen is told about each order on its delivery day

Revision ID: 0008_kitchen_alerts
Revises: 0007_uber_queue
Create Date: 2026-09-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_kitchen_alerts"
down_revision = "0007_uber_queue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("kitchen_notify_at", sa.DateTime(timezone=True),
                                      nullable=True))
    op.add_column("orders", sa.Column("kitchen_notified_at", sa.DateTime(timezone=True),
                                      nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "kitchen_notified_at")
    op.drop_column("orders", "kitchen_notify_at")
