"""Zoho links for dishes, kitchens and order lines

Menu dishes are synced to Zoho Products and kitchens to Zoho Vendors, and
every dish on a paid order becomes an Order Item record there. The local rows
remember the Zoho ids so a later sync updates rather than duplicates.

Revision ID: 0004_zoho_catalogue_links
Revises: 0003_customer_addresses
Create Date: 2026-09-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_zoho_catalogue_links"
down_revision = "0003_customer_addresses"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("menu_items", sa.Column("zoho_product_id", sa.String(length=40),
                                          nullable=True))
    op.create_index("ix_menu_items_zoho_product_id", "menu_items", ["zoho_product_id"])

    op.add_column("outlets", sa.Column("zoho_vendor_id", sa.String(length=40),
                                       nullable=True))
    op.create_index("ix_outlets_zoho_vendor_id", "outlets", ["zoho_vendor_id"])

    # server_default so the ALTER works on rows that already exist.
    op.add_column("orders", sa.Column("zoho_item_ids", postgresql.JSONB(), nullable=False,
                                      server_default=sa.text("'{}'")))


def downgrade() -> None:
    op.drop_column("orders", "zoho_item_ids")
    op.drop_index("ix_outlets_zoho_vendor_id", table_name="outlets")
    op.drop_column("outlets", "zoho_vendor_id")
    op.drop_index("ix_menu_items_zoho_product_id", table_name="menu_items")
    op.drop_column("menu_items", "zoho_product_id")
