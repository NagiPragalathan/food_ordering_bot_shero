"""Retargeting nudges: campaigns, global settings, the send log, opt-out

Revision ID: 0012_nudges
Revises: 0011_loyalty
Create Date: 2026-10-22
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from app.db.types import json_server_default

revision = "0012_nudges"
down_revision = "0011_loyalty"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    ]


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    json_type = JSONB() if dialect == "postgresql" else sa.JSON()

    op.create_table(
        "nudge_settings",
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("values", json_type, nullable=False,
                  server_default=json_server_default("{}", dialect)),
        sa.Column("updated_by", sa.String(length=120), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "nudge_campaigns",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("promotion_id", sa.Uuid(), nullable=True),
        sa.Column("config", json_type, nullable=False,
                  server_default=json_server_default("{}", dialect)),
        sa.Column("created_by", sa.String(length=120), nullable=True),
        sa.Column("updated_by", sa.String(length=120), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["promotion_id"], ["promotions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_nudge_campaigns_kind", "nudge_campaigns", ["kind"])
    op.create_index("ix_nudge_campaigns_promotion_id", "nudge_campaigns", ["promotion_id"])

    op.create_table(
        "nudge_sends",
        sa.Column("campaign_id", sa.Uuid(), nullable=True),
        sa.Column("campaign_name", sa.String(length=120), nullable=True),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("whatsapp_number", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=False),
        sa.Column("claim_key", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("promo_code", sa.String(length=32), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("zoho_synced", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("zoho_error", sa.String(length=500), nullable=True),
        sa.Column("actor", sa.String(length=120), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["campaign_id"], ["nudge_campaigns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("claim_key", name="uq_nudge_sends_claim_key"),
    )
    op.create_index("ix_nudge_sends_campaign_id", "nudge_sends", ["campaign_id"])
    op.create_index("ix_nudge_sends_customer_id", "nudge_sends", ["customer_id"])
    op.create_index("ix_nudge_sends_status", "nudge_sends", ["status"])
    op.create_index("ix_nudge_sends_sent_at", "nudge_sends", ["sent_at"])

    op.add_column("customers", sa.Column("nudges_opted_out_at", sa.DateTime(timezone=True),
                                         nullable=True))


def downgrade() -> None:
    op.drop_column("customers", "nudges_opted_out_at")
    op.drop_index("ix_nudge_sends_sent_at", table_name="nudge_sends")
    op.drop_index("ix_nudge_sends_status", table_name="nudge_sends")
    op.drop_index("ix_nudge_sends_customer_id", table_name="nudge_sends")
    op.drop_index("ix_nudge_sends_campaign_id", table_name="nudge_sends")
    op.drop_table("nudge_sends")
    op.drop_index("ix_nudge_campaigns_promotion_id", table_name="nudge_campaigns")
    op.drop_index("ix_nudge_campaigns_kind", table_name="nudge_campaigns")
    op.drop_table("nudge_campaigns")
    op.drop_table("nudge_settings")
