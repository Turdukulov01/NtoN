"""Add KYT provider report cache.

Revision ID: 20260701_0003
Revises: 20260701_0002
Create Date: 2026-07-01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260701_0003"
down_revision = "20260701_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kyt_provider_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("network", sa.String(length=32), nullable=False),
        sa.Column("address", sa.String(length=128), nullable=False),
        sa.Column("normalized_address", sa.String(length=128), nullable=False),
        sa.Column("provider_score", sa.Numeric(7, 2), nullable=True),
        sa.Column("provider_risk_level", sa.String(length=64), nullable=True),
        sa.Column("tx_total", sa.BigInteger(), nullable=True),
        sa.Column("activity_first", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activity_last", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_response", postgresql.JSONB(), nullable=False),
        sa.Column("normalized_payload", postgresql.JSONB(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_kyt_provider_reports_provider", "kyt_provider_reports", ["provider"])
    op.create_index("ix_kyt_provider_reports_network", "kyt_provider_reports", ["network"])
    op.create_index("ix_kyt_provider_reports_normalized_address", "kyt_provider_reports", ["normalized_address"])
    op.create_index("ix_kyt_provider_reports_fetched_at", "kyt_provider_reports", ["fetched_at"])
    op.create_index("ix_kyt_provider_reports_expires_at", "kyt_provider_reports", ["expires_at"])
    op.create_index(
        "ix_kyt_report_lookup",
        "kyt_provider_reports",
        ["provider", "network", "normalized_address", "expires_at"],
    )
    op.create_index(
        "ix_kyt_report_address_fetched",
        "kyt_provider_reports",
        ["network", "normalized_address", "fetched_at"],
    )

    op.create_table(
        "kyt_exposures",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kyt_provider_reports.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("network", sa.String(length=32), nullable=False),
        sa.Column("address", sa.String(length=128), nullable=False),
        sa.Column("normalized_address", sa.String(length=128), nullable=False),
        sa.Column("category_name", sa.String(length=255), nullable=False),
        sa.Column("category_key", sa.String(length=255), nullable=False),
        sa.Column("exposure_group", sa.String(length=64), nullable=True),
        sa.Column("percent", sa.Numeric(9, 4), nullable=False),
        sa.Column("amount", sa.Text(), nullable=True),
        sa.Column("amount_human", sa.String(length=128), nullable=True),
        sa.Column("provider_risk_score", sa.Numeric(9, 4), nullable=True),
        sa.Column("model_category_score", sa.Numeric(9, 4), nullable=True),
        sa.Column("model_contribution_score", sa.Numeric(9, 4), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_kyt_exposures_provider", "kyt_exposures", ["provider"])
    op.create_index("ix_kyt_exposures_network", "kyt_exposures", ["network"])
    op.create_index("ix_kyt_exposures_normalized_address", "kyt_exposures", ["normalized_address"])
    op.create_index("ix_kyt_exposures_category_key", "kyt_exposures", ["category_key"])
    op.create_index("ix_kyt_exposures_exposure_group", "kyt_exposures", ["exposure_group"])
    op.create_index("ix_kyt_exposure_report", "kyt_exposures", ["report_id"])
    op.create_index(
        "ix_kyt_exposure_lookup",
        "kyt_exposures",
        ["provider", "network", "normalized_address", "category_key"],
    )
    op.create_index("ix_kyt_exposure_group_percent", "kyt_exposures", ["exposure_group", "percent"])


def downgrade() -> None:
    op.drop_index("ix_kyt_exposure_group_percent", table_name="kyt_exposures")
    op.drop_index("ix_kyt_exposure_lookup", table_name="kyt_exposures")
    op.drop_index("ix_kyt_exposure_report", table_name="kyt_exposures")
    op.drop_index("ix_kyt_exposures_exposure_group", table_name="kyt_exposures")
    op.drop_index("ix_kyt_exposures_category_key", table_name="kyt_exposures")
    op.drop_index("ix_kyt_exposures_normalized_address", table_name="kyt_exposures")
    op.drop_index("ix_kyt_exposures_network", table_name="kyt_exposures")
    op.drop_index("ix_kyt_exposures_provider", table_name="kyt_exposures")
    op.drop_table("kyt_exposures")

    op.drop_index("ix_kyt_report_address_fetched", table_name="kyt_provider_reports")
    op.drop_index("ix_kyt_report_lookup", table_name="kyt_provider_reports")
    op.drop_index("ix_kyt_provider_reports_expires_at", table_name="kyt_provider_reports")
    op.drop_index("ix_kyt_provider_reports_fetched_at", table_name="kyt_provider_reports")
    op.drop_index("ix_kyt_provider_reports_normalized_address", table_name="kyt_provider_reports")
    op.drop_index("ix_kyt_provider_reports_network", table_name="kyt_provider_reports")
    op.drop_index("ix_kyt_provider_reports_provider", table_name="kyt_provider_reports")
    op.drop_table("kyt_provider_reports")
