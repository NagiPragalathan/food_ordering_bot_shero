"""Loyalty wallets, transaction history and order snapshots."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from alembic import op
revision = "0011_loyalty"
down_revision = "0010_promotions"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("loyalty_accounts",
        sa.Column("customer_id", sa.Uuid(), sa.ForeignKey("customers.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("balance", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("eligible_orders", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table("loyalty_entries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("customer_id", sa.Uuid(), sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_id", sa.Uuid(), sa.ForeignKey("orders.id", ondelete="SET NULL")),
        sa.Column("event_key", sa.String(120), nullable=False, unique=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("points", sa.BigInteger(), nullable=False),
        sa.Column("balance_after", sa.BigInteger(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_loyalty_entries_customer_id", "loyalty_entries", ["customer_id"])
    op.create_index("ix_loyalty_entries_order_id", "loyalty_entries", ["order_id"])
    for name in ("loyalty_points", "loyalty_earned"):
        op.add_column("orders", sa.Column(name, sa.BigInteger() if name == "loyalty_earned" else sa.Integer(), nullable=False, server_default="0"))
    op.add_column("orders", sa.Column("loyalty_discount", sa.Numeric(10,2), nullable=False, server_default="0"))
    op.add_column("orders", sa.Column("loyalty_state", sa.String(16), nullable=False, server_default="none"))
    json_type = JSONB() if op.get_bind().dialect.name == "postgresql" else sa.JSON()
    op.add_column("orders", sa.Column("loyalty_policy", json_type, nullable=False, server_default=sa.text("'{}'")))

def downgrade():
    for name in ("loyalty_policy", "loyalty_state", "loyalty_discount", "loyalty_earned", "loyalty_points"):
        op.drop_column("orders", name)
    op.drop_index("ix_loyalty_entries_order_id", table_name="loyalty_entries")
    op.drop_index("ix_loyalty_entries_customer_id", table_name="loyalty_entries")
    op.drop_table("loyalty_entries")
    op.drop_table("loyalty_accounts")
