"""When a customer was last told on WhatsApp that we do not deliver to them

The out-of-area message is sent at most once a day per customer, however
often they try another address (services/out_of_area.py).

Revision ID: 0009_out_of_area_notice
Revises: 0008_kitchen_alerts
Create Date: 2026-10-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_out_of_area_notice"
down_revision = "0008_kitchen_alerts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("out_of_area_notified_at",
                                         sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("customers", "out_of_area_notified_at")
