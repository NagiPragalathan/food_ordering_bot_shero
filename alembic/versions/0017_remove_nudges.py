"""Remove the nudges feature: its campaigns, message library, sends, follow-up
journeys, held-back log and settings, and the team's hold on a customer

customers.nudges_opted_out_at stays: who replied STOP to an offer.

Revision ID: 0017_remove_nudges
Revises: 0016_smo_handoff
Create Date: 2026-10-31
"""
from __future__ import annotations

from alembic import op

revision = "0017_remove_nudges"
down_revision = "0016_smo_handoff"
branch_labels = None
depends_on = None

# Each table before the ones it points at.
TABLES = ("nudge_skips", "nudge_journeys", "nudge_sends", "nudge_messages",
          "nudge_campaigns", "nudge_settings")
HOLD_COLUMNS = ("nudge_hold", "nudge_hold_note", "nudge_hold_at", "nudge_hold_by")


def upgrade() -> None:
    for table in TABLES:
        op.drop_table(table)
    # A plain ALTER TABLE ... DROP COLUMN, not a batch rebuild: rebuilding
    # customers on SQLite would drop and recreate the table that orders,
    # addresses and conversations point at.
    for name in HOLD_COLUMNS:
        op.drop_column("customers", name)


def downgrade() -> None:
    raise NotImplementedError(
        "The nudges tables are gone with their rows; restore the database "
        "backup taken before this migration instead.")
