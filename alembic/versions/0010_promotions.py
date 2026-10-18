"""Promotions (coupon codes) and the discount on an order

Adds the promotions table behind the admin Offers page, and the order's
discount with a snapshot of the code and title it came from.

Revision ID: 0010_promotions
Revises: 0009_out_of_area_notice
Create Date: 2026-10-18
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_promotions"
down_revision = "0009_out_of_area_notice"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "promotions",
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("tag", sa.String(length=40), nullable=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("discount_type", sa.String(length=20), nullable=False,
                  server_default="percent"),
        sa.Column("discount_value", sa.Numeric(10, 2), nullable=False,
                  server_default="0"),
        sa.Column("min_order", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("max_discount", sa.Numeric(10, 2), nullable=True),
        sa.Column("usage_limit", sa.Integer(), nullable=True),
        sa.Column("per_customer_limit", sa.Integer(), nullable=True),
        sa.Column("first_order_only", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("outlet_id", sa.Uuid(), nullable=True),
        sa.Column("starts_on", sa.Date(), nullable=True),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("placement", sa.String(length=20), nullable=False,
                  server_default="checkout"),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(length=120), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["outlet_id"], ["outlets.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_promotions_code", "promotions", ["code"], unique=True)

    op.add_column("orders", sa.Column("discount", sa.Numeric(10, 2), nullable=False,
                                      server_default="0"))
    op.add_column("orders", sa.Column("promotion_id", sa.Uuid(), nullable=True))
    op.add_column("orders", sa.Column("promo_code", sa.String(length=32), nullable=True))
    op.add_column("orders", sa.Column("promo_title", sa.String(length=120), nullable=True))
    # SQLite (a local run) cannot add a constraint to an existing table; the
    # column is enough there. Postgres gets the real foreign key.
    if op.get_bind().dialect.name != "sqlite":
        op.create_foreign_key("fk_orders_promotion_id", "orders", "promotions",
                              ["promotion_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_orders_promotion_id", "orders", ["promotion_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_orders_promotion_id", table_name="orders")
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint("fk_orders_promotion_id", "orders", type_="foreignkey")
    op.drop_column("orders", "promo_title")
    op.drop_column("orders", "promo_code")
    op.drop_column("orders", "promotion_id")
    op.drop_column("orders", "discount")
    op.drop_index("ix_promotions_code", table_name="promotions")
    op.drop_table("promotions")
