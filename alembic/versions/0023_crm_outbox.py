"""Events on their way to DST India's own CRM

Revision ID: 0023_crm_outbox
Revises: 0022_page_comments
Create Date: 2026-11-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023_crm_outbox"
down_revision = "0022_page_comments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "crm_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event", sa.String(length=30), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("result", sa.String(length=500), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_crm_outbox_dedupe_key"),
    )
    op.create_index("ix_crm_outbox_customer_id", "crm_outbox", ["customer_id"])
    op.create_index("ix_crm_outbox_status", "crm_outbox", ["status"])
    op.create_index("ix_crm_outbox_next_attempt_at", "crm_outbox", ["next_attempt_at"])
    op.create_index("ix_crm_outbox_queued_at", "crm_outbox", ["queued_at"])


def downgrade() -> None:
    op.drop_index("ix_crm_outbox_queued_at", table_name="crm_outbox")
    op.drop_index("ix_crm_outbox_next_attempt_at", table_name="crm_outbox")
    op.drop_index("ix_crm_outbox_status", table_name="crm_outbox")
    op.drop_index("ix_crm_outbox_customer_id", table_name="crm_outbox")
    op.drop_table("crm_outbox")
