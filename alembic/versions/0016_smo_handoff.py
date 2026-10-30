"""SMO handoff: occasion area and cutoff, the ordering flow and time zone on a
journey, delivery tracking on a send, the customer hold, the held-back log

Revision ID: 0016_smo_handoff
Revises: 0015_partner_payouts
Create Date: 2026-10-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from app.db.types import json_server_default

revision = "0016_smo_handoff"
down_revision = "0015_partner_payouts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    json_type = JSONB() if dialect == "postgresql" else sa.JSON()

    op.add_column("nudge_messages", sa.Column("kitchens", json_type, nullable=False,
                                              server_default=json_server_default("[]", dialect)))
    op.add_column("nudge_messages", sa.Column("order_by", sa.Date(), nullable=True))

    for column in (
        sa.Column("flow_status", sa.String(length=16), nullable=False, server_default="not_started"),
        sa.Column("flow_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_keyword", sa.String(length=60), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("timezone_source", sa.String(length=16), nullable=True),
        sa.Column("last_skip_reason", sa.String(length=60), nullable=True),
        sa.Column("last_skip_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("zoho_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("zoho_error", sa.String(length=300), nullable=True),
    ):
        op.add_column("nudge_journeys", column)

    op.add_column("nudge_sends", sa.Column("provider_message_id", sa.String(length=120), nullable=True))
    op.add_column("nudge_sends", sa.Column("delivery_status", sa.String(length=20), nullable=True))
    op.add_column("nudge_sends", sa.Column("delivery_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("nudge_sends", sa.Column("keyword_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_nudge_sends_provider_message_id", "nudge_sends", ["provider_message_id"])

    op.add_column("customers", sa.Column("nudge_hold", sa.String(length=24), nullable=True))
    op.add_column("customers", sa.Column("nudge_hold_note", sa.String(length=300), nullable=True))
    op.add_column("customers", sa.Column("nudge_hold_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("customers", sa.Column("nudge_hold_by", sa.String(length=120), nullable=True))

    op.create_table(
        "nudge_skips",
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(length=60), nullable=False),
        sa.Column("message_code", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["nudge_campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "customer_id", "day", "reason", "message_code",
                            name="uq_nudge_skips_day"),
    )
    op.create_index("ix_nudge_skips_campaign_id", "nudge_skips", ["campaign_id"])
    op.create_index("ix_nudge_skips_customer_id", "nudge_skips", ["customer_id"])
    op.create_index("ix_nudge_skips_day", "nudge_skips", ["day"])


def downgrade() -> None:
    op.drop_index("ix_nudge_skips_day", table_name="nudge_skips")
    op.drop_index("ix_nudge_skips_customer_id", table_name="nudge_skips")
    op.drop_index("ix_nudge_skips_campaign_id", table_name="nudge_skips")
    op.drop_table("nudge_skips")
    for name in ("nudge_hold_by", "nudge_hold_at", "nudge_hold_note", "nudge_hold"):
        op.drop_column("customers", name)
    op.drop_index("ix_nudge_sends_provider_message_id", table_name="nudge_sends")
    for name in ("keyword_at", "delivery_at", "delivery_status", "provider_message_id"):
        op.drop_column("nudge_sends", name)
    for name in ("zoho_error", "zoho_synced_at", "last_skip_at", "last_skip_reason",
                 "timezone_source", "timezone", "last_keyword", "flow_started_at", "flow_status"):
        op.drop_column("nudge_journeys", name)
    op.drop_column("nudge_messages", "order_by")
    op.drop_column("nudge_messages", "kitchens")
