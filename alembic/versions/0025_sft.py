"""Shero Food Training (SFT): course leads and their timeline

Revision ID: 0025_sft
Revises: 0024_shiprocket_ready
Create Date: 2026-11-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0025_sft"
down_revision = "0024_shiprocket_ready"
branch_labels = None
depends_on = None


def _json(name: str, default: str) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                     server_default=sa.text(f"'{default}'"))


def _when(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def upgrade() -> None:
    op.create_table(
        "sft_leads",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("whatsapp_number", sa.String(20), nullable=False),
        sa.Column("name", sa.String(120)),
        sa.Column("profile_name", sa.String(120)),
        sa.Column("email", sa.String(255)),
        sa.Column("goal", sa.String(20)),
        sa.Column("stage", sa.String(30), nullable=False, server_default="new"),
        sa.Column("step", sa.String(30), nullable=False, server_default="idle"),
        _json("stage_times", "{}"),
        sa.Column("source", sa.String(60), nullable=False, server_default="WhatsApp"),
        sa.Column("source_detail", sa.String(200)),
        sa.Column("ad_id", sa.String(64)),
        sa.Column("campaign_id", sa.String(64)),
        sa.Column("first_message", sa.String(300)),
        sa.Column("channel_id", sa.String(64)),
        _when("last_inbound_at"),
        _when("last_outbound_at"),
        _when("last_activity_at"),
        sa.Column("followups_sent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("nurture_sent", sa.Integer(), nullable=False, server_default="0"),
        _when("nurture_until"),
        sa.Column("opted_out", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("lost_reason", sa.String(60)),
        sa.Column("with_team", sa.Boolean(), nullable=False, server_default=sa.false()),
        _when("handed_over_at"),
        sa.Column("question", sa.Text()),
        sa.Column("needs_attention", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("attention_reason", sa.String(200)),
        sa.Column("attention_priority", sa.String(10)),
        sa.Column("attention_stage", sa.String(30)),
        _when("attention_since"),
        _json("attention_raised", "[]"),
        sa.Column("pay_token", sa.String(32), nullable=False),
        sa.Column("checkout_session_id", sa.String(255)),
        sa.Column("checkout_url", sa.Text()),
        _when("checkout_expires_at"),
        _when("link_sent_at"),
        _when("link_clicked_at"),
        _when("payment_reminded_at"),
        _when("paid_at"),
        sa.Column("amount_paid", sa.Numeric(10, 2)),
        sa.Column("currency", sa.String(3)),
        sa.Column("payment_intent_id", sa.String(255)),
        sa.Column("test_payment", sa.Boolean(), nullable=False, server_default=sa.false()),
        _when("refunded_at"),
        _when("course_link_sent_at"),
        _when("access_check_sent_at"),
        _when("access_confirmed_at"),
        sa.Column("zoho_module", sa.String(20)),
        sa.Column("zoho_id", sa.String(32)),
        sa.Column("zoho_created", sa.Boolean(), nullable=False, server_default=sa.false()),
        _json("zoho_tags", "[]"),
        sa.Column("crm_dirty", sa.Boolean(), nullable=False, server_default=sa.true()),
        _when("crm_next_try_at"),
        sa.Column("crm_attempts", sa.Integer(), nullable=False, server_default="0"),
        _when("crm_synced_at"),
        sa.Column("crm_error", sa.String(500)),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_sft_leads_whatsapp_number", "sft_leads", ["whatsapp_number"], unique=True)
    op.create_index("ix_sft_leads_pay_token", "sft_leads", ["pay_token"], unique=True)
    op.create_index("ix_sft_leads_stage", "sft_leads", ["stage"])
    op.create_index("ix_sft_leads_last_activity_at", "sft_leads", ["last_activity_at"])
    op.create_index("ix_sft_leads_with_team", "sft_leads", ["with_team"])
    op.create_index("ix_sft_leads_needs_attention", "sft_leads", ["needs_attention"])
    op.create_index("ix_sft_leads_crm_dirty", "sft_leads", ["crm_dirty"])

    op.create_table(
        "sft_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("lead_id", sa.Uuid(), sa.ForeignKey("sft_leads.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("stage", sa.String(30)),
        sa.Column("detail", sa.String(600)),
        _json("data", "{}"),
        sa.Column("crm_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_sft_events_lead_id", "sft_events", ["lead_id"])
    op.create_index("ix_sft_events_at", "sft_events", ["at"])
    op.create_index("ix_sft_events_kind", "sft_events", ["kind"])
    op.create_index("ix_sft_events_crm_pending", "sft_events", ["crm_pending"])


def downgrade() -> None:
    op.drop_table("sft_events")
    op.drop_table("sft_leads")
