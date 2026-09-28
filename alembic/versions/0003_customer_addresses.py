"""saved delivery addresses per customer

Adds customer_addresses, so the web ordering page can offer Home / Office /
other saved addresses instead of one address overwritten on every order.

Revision ID: 0003_customer_addresses
Revises: 0002_menu_and_admin
Create Date: 2026-09-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_customer_addresses"
down_revision = "0002_menu_and_admin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_addresses",
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=40), nullable=False),
        sa.Column("address_line1", sa.String(length=255), nullable=False),
        sa.Column("apartment_unit", sa.String(length=120), nullable=True),
        sa.Column("postal_code", sa.String(length=20), nullable=False),
        sa.Column("delivery_instructions", sa.String(length=500), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_customer_addresses_customer_id", "customer_addresses",
                    ["customer_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_customer_addresses_customer_id", table_name="customer_addresses")
    op.drop_table("customer_addresses")
