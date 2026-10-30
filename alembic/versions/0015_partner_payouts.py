"""Kitchen partner payouts: each order's split, and payments made to kitchens

Revision ID: 0015_partner_payouts
Revises: 0014_followup_journeys
Create Date: 2026-10-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from app.db.types import json_server_default

revision = "0015_partner_payouts"
down_revision = "0014_followup_journeys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    json_type = JSONB() if dialect == "postgresql" else sa.JSON()

    op.create_table(
        "partner_payouts",
        sa.Column("outlet_id", sa.Uuid(), nullable=True),
        sa.Column("outlet_name", sa.String(length=120), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("order_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_on", sa.Date(), nullable=False),
        sa.Column("reference", sa.String(length=120), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("created_by", sa.String(length=120), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voided_by", sa.String(length=120), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.ForeignKeyConstraint(["outlet_id"], ["outlets.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_partner_payouts_outlet_id", "partner_payouts", ["outlet_id"])

    op.add_column("orders", sa.Column("payout_split", json_type, nullable=False,
                                      server_default=json_server_default("{}", dialect)))
    op.add_column("orders", sa.Column("payout_id", sa.Uuid(), nullable=True))
    # SQLite cannot add a constraint to an existing table; the column is
    # enough there. Postgres gets the real foreign key.
    if dialect != "sqlite":
        op.create_foreign_key("fk_orders_payout_id", "orders", "partner_payouts",
                              ["payout_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_orders_payout_id", "orders", ["payout_id"])


def downgrade() -> None:
    op.drop_index("ix_orders_payout_id", table_name="orders")
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint("fk_orders_payout_id", "orders", type_="foreignkey")
    op.drop_column("orders", "payout_id")
    op.drop_column("orders", "payout_split")
    op.drop_index("ix_partner_payouts_outlet_id", table_name="partner_payouts")
    op.drop_table("partner_payouts")
