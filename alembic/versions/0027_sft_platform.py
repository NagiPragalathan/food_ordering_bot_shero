"""SFT: the student's account on the Shero Training course platform
(api.sherosft.com), made once they have paid

Revision ID: 0027_sft_platform
Revises: 0026_sft_offers
Create Date: 2026-11-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027_sft_platform"
down_revision = "0026_sft_offers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("sft_leads") as batch:
        batch.add_column(sa.Column("platform_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("platform_enrolled_at", sa.DateTime(timezone=True),
                                   nullable=True))
        batch.add_column(sa.Column("platform_error", sa.String(300), nullable=True))
        batch.add_column(sa.Column("platform_attempts", sa.Integer(), nullable=False,
                                   server_default="0"))
        batch.add_column(sa.Column("platform_next_try_at", sa.DateTime(timezone=True),
                                   nullable=True))
        batch.add_column(sa.Column("login_sent_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_sft_leads_platform_next_try_at", ["platform_next_try_at"])


def downgrade() -> None:
    with op.batch_alter_table("sft_leads") as batch:
        batch.drop_index("ix_sft_leads_platform_next_try_at")
        batch.drop_column("login_sent_at")
        batch.drop_column("platform_next_try_at")
        batch.drop_column("platform_attempts")
        batch.drop_column("platform_error")
        batch.drop_column("platform_enrolled_at")
        batch.drop_column("platform_id")
