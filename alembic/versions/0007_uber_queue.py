"""When each paid order is sent to Uber

Revision ID: 0007_uber_queue
Revises: 0006_uber_dispatch
Create Date: 2026-09-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_uber_queue"
down_revision = "0006_uber_dispatch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("uber_dispatch_due_at", sa.DateTime(timezone=True),
                                      nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "uber_dispatch_due_at")
