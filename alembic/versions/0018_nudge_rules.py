"""Nudges by stage: each customer's place in the retargeting, and every
message sent

Revision ID: 0018_nudge_rules
Revises: 0017_remove_nudges
Create Date: 2026-10-31
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_nudge_rules"
down_revision = "0017_remove_nudges"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                      nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                      nullable=False)]


def upgrade() -> None:
    op.create_table(
        "nudge_leads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("stage", sa.String(length=40), nullable=False),
        sa.Column("stage_entered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stage_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bucket", sa.String(length=16), nullable=True),
        sa.Column("bucket_from", sa.String(length=40), nullable=True),
        sa.Column("bucket_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("waiting", sa.String(length=200), nullable=True),
        sa.Column("last_reply", sa.String(length=20), nullable=True),
        sa.Column("last_reply_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("zoho_error", sa.String(length=300), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("customer_id"),
    )
    op.create_index("ix_nudge_leads_bucket", "nudge_leads", ["bucket"])
    op.create_index("ix_nudge_leads_next_due_at", "nudge_leads", ["next_due_at"])

    op.create_table(
        "nudge_sends",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("stage", sa.String(length=40), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("template", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_message_id", sa.String(length=120), nullable=True),
        sa.Column("delivery_status", sa.String(length=20), nullable=True),
        sa.Column("delivery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reply", sa.String(length=20), nullable=True),
        sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_nudge_sends_customer_id", "nudge_sends", ["customer_id"])
    op.create_index("ix_nudge_sends_sent_at", "nudge_sends", ["sent_at"])
    op.create_index("ix_nudge_sends_provider_message_id", "nudge_sends", ["provider_message_id"])


def downgrade() -> None:
    op.drop_index("ix_nudge_sends_provider_message_id", table_name="nudge_sends")
    op.drop_index("ix_nudge_sends_sent_at", table_name="nudge_sends")
    op.drop_index("ix_nudge_sends_customer_id", table_name="nudge_sends")
    op.drop_table("nudge_sends")
    op.drop_index("ix_nudge_leads_next_due_at", table_name="nudge_leads")
    op.drop_index("ix_nudge_leads_bucket", table_name="nudge_leads")
    op.drop_table("nudge_leads")
