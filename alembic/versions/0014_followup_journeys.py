"""Follow-up journeys: the message library, each customer's place, the message sent

Revision ID: 0014_followup_journeys
Revises: 0013_order_zoho_note
Create Date: 2026-10-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from app.db.types import json_server_default

revision = "0014_followup_journeys"
down_revision = "0013_order_zoho_note"
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

    def json_list(name: str) -> sa.Column:
        return sa.Column(name, json_type, nullable=False,
                         server_default=json_server_default("[]", dialect))

    op.create_table(
        "nudge_messages",
        sa.Column("code", sa.String(length=40), nullable=False),
        sa.Column("pool", sa.String(length=60), nullable=False, server_default="SMO"),
        sa.Column("month", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("angle", sa.String(length=80), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        json_list("buckets"),
        sa.Column("occasion", sa.String(length=80), nullable=True),
        sa.Column("starts_on", sa.Date(), nullable=True),
        sa.Column("ends_on", sa.Date(), nullable=True),
        json_list("dishes"),
        sa.Column("wa_template", sa.String(length=120), nullable=True),
        json_list("wa_params"),
        sa.Column("wa_button", sa.String(length=16), nullable=False, server_default="none"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(length=120), nullable=True),
        sa.Column("updated_by", sa.String(length=120), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_nudge_messages_code"),
    )
    op.create_index("ix_nudge_messages_pool", "nudge_messages", ["pool"])

    op.create_table(
        "nudge_journeys",
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("bucket", sa.String(length=16), nullable=False, server_default="no_response"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("round", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bucket_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_message_code", sa.String(length=40), nullable=True),
        json_list("recent"),
        sa.Column("last_reply", sa.String(length=16), nullable=True),
        sa.Column("last_reply_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(length=40), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["campaign_id"], ["nudge_campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "customer_id",
                            name="uq_nudge_journeys_campaign_customer"),
    )
    op.create_index("ix_nudge_journeys_campaign_id", "nudge_journeys", ["campaign_id"])
    op.create_index("ix_nudge_journeys_customer_id", "nudge_journeys", ["customer_id"])
    op.create_index("ix_nudge_journeys_status", "nudge_journeys", ["status"])
    op.create_index("ix_nudge_journeys_next_due_at", "nudge_journeys", ["next_due_at"])

    op.add_column("nudge_sends", sa.Column("message_code", sa.String(length=40), nullable=True))
    op.add_column("nudge_sends", sa.Column("note", sa.String(length=200), nullable=True))
    op.create_index("ix_nudge_sends_message_code", "nudge_sends", ["message_code"])


def downgrade() -> None:
    op.drop_index("ix_nudge_sends_message_code", table_name="nudge_sends")
    op.drop_column("nudge_sends", "note")
    op.drop_column("nudge_sends", "message_code")
    op.drop_index("ix_nudge_journeys_next_due_at", table_name="nudge_journeys")
    op.drop_index("ix_nudge_journeys_status", table_name="nudge_journeys")
    op.drop_index("ix_nudge_journeys_customer_id", table_name="nudge_journeys")
    op.drop_index("ix_nudge_journeys_campaign_id", table_name="nudge_journeys")
    op.drop_table("nudge_journeys")
    op.drop_index("ix_nudge_messages_pool", table_name="nudge_messages")
    op.drop_table("nudge_messages")
