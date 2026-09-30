"""Uber courier booking per order

Revision ID: 0006_uber_dispatch
Revises: 0005_profile_name_and_sales_orders
Create Date: 2026-09-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_uber_dispatch"
down_revision = "0005_profile_name_and_sales_orders"
branch_labels = None
depends_on = None

COLUMNS = (
    ("uber_delivery_id", sa.String(length=128)),
    ("uber_delivery_status", sa.String(length=40)),
    ("uber_tracking_url", sa.Text()),
    ("uber_dispatched_at", sa.DateTime(timezone=True)),
    ("uber_dispatch_error", sa.String(length=500)),
)


def upgrade() -> None:
    for name, kind in COLUMNS:
        op.add_column("orders", sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(COLUMNS):
        op.drop_column("orders", name)
