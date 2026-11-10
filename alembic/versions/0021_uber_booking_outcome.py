"""Uber bookings: when the job stopped retrying one Uber refused for good,
and what Uber charged for a booked delivery

Revision ID: 0021_uber_booking_outcome
Revises: 0020_nudge_zoho_records
Create Date: 2026-11-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_uber_booking_outcome"
down_revision = "0020_nudge_zoho_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("uber_retry_stopped_at", sa.DateTime(timezone=True),
                                   nullable=True))
        batch.add_column(sa.Column("uber_fee", sa.Numeric(10, 2), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.drop_column("uber_fee")
        batch.drop_column("uber_retry_stopped_at")
