"""Comments on the dashboard's pages, and who may leave them

Revision ID: 0022_page_comments
Revises: 0021_uber_booking_outcome
Create Date: 2026-11-16
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_page_comments"
down_revision = "0021_uber_booking_outcome"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "page_comments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("business", sa.String(length=20), nullable=False),
        sa.Column("path", sa.String(length=500), nullable=False),
        sa.Column("query", sa.String(length=500), nullable=True),
        sa.Column("page_title", sa.String(length=200), nullable=True),
        sa.Column("template", sa.String(length=200), nullable=True),
        sa.Column("route_file", sa.String(length=200), nullable=True),
        sa.Column("selector", sa.Text(), nullable=True),
        sa.Column("element_label", sa.String(length=300), nullable=True),
        sa.Column("element_html", sa.Text(), nullable=True),
        sa.Column("x_pct", sa.Float(), nullable=True),
        sa.Column("y_pct", sa.Float(), nullable=True),
        sa.Column("page_x", sa.Float(), nullable=True),
        sa.Column("page_y", sa.Float(), nullable=True),
        sa.Column("viewport_width", sa.Integer(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author_email", sa.String(length=255), nullable=False),
        sa.Column("author_name", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("resolved_by", sa.String(length=255), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["parent_id"], ["page_comments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_page_comments_parent_id", "page_comments", ["parent_id"])
    op.create_index("ix_page_comments_path", "page_comments", ["path"])
    op.create_table(
        "page_comment_reviewers",
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("added_by", sa.String(length=255), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("email"),
    )


def downgrade() -> None:
    op.drop_table("page_comment_reviewers")
    op.drop_index("ix_page_comments_path", table_name="page_comments")
    op.drop_index("ix_page_comments_parent_id", table_name="page_comments")
    op.drop_table("page_comments")
