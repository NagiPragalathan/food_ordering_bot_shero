"""SFT offers: codes and launch prices for the course, the offer on a lead, and
the time of a lead's last nudge

Revision ID: 0026_sft_offers
Revises: 0025_sft
Create Date: 2026-11-23
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0026_sft_offers"
down_revision = "0025_sft"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sft_offers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("title", sa.String(80), nullable=False),
        sa.Column("code", sa.String(32)),
        sa.Column("kind", sa.String(10), nullable=False, server_default="percent"),
        sa.Column("value", sa.Numeric(10, 2), nullable=False),
        sa.Column("starts_on", sa.Date()),
        sa.Column("ends_on", sa.Date()),
        sa.Column("usage_limit", sa.Integer()),
        sa.Column("sources", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                  server_default=sa.text("'[]'")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("note", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_sft_offers_code", "sft_offers", ["code"])

    with op.batch_alter_table("sft_leads") as batch:
        batch.add_column(sa.Column("offer_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("offer_code", sa.String(32), nullable=True))
        batch.add_column(sa.Column("checkout_amount", sa.Numeric(10, 2), nullable=True))
        batch.add_column(sa.Column("nudged_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_sft_leads_offer_id", ["offer_id"])


def downgrade() -> None:
    with op.batch_alter_table("sft_leads") as batch:
        batch.drop_index("ix_sft_leads_offer_id")
        batch.drop_column("nudged_at")
        batch.drop_column("checkout_amount")
        batch.drop_column("offer_code")
        batch.drop_column("offer_id")
    op.drop_index("ix_sft_offers_code", table_name="sft_offers")
    op.drop_table("sft_offers")
