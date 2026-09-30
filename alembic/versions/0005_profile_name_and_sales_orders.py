"""WhatsApp profile name, and the Zoho Sales Order link per order

Revision ID: 0005_profile_name_and_sales_orders
Revises: 0004_zoho_catalogue_links
Create Date: 2026-09-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_profile_name_and_sales_orders"
down_revision = "0004_zoho_catalogue_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("whatsapp_profile_name", sa.String(length=120),
                                         nullable=True))
    op.add_column("orders", sa.Column("zoho_sales_order_id", sa.String(length=40),
                                      nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "zoho_sales_order_id")
    op.drop_column("customers", "whatsapp_profile_name")
