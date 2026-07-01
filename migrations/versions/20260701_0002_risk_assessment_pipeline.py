"""Add persistent risk assessment pipeline tables.

Revision ID: 20260701_0002
Revises: 20260630_0001
Create Date: 2026-07-01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260701_0002"
down_revision = "20260630_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "address_risk_tags",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("network", sa.String(length=32), nullable=False),
        sa.Column("address", sa.String(length=128), nullable=False),
        sa.Column("normalized_address", sa.String(length=128), nullable=False),
        sa.Column("tag", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 2), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("network", "normalized_address", "tag", "source", name="uq_address_risk_tag_source"),
    )
    op.create_index("ix_address_risk_tags_network", "address_risk_tags", ["network"])
    op.create_index("ix_address_risk_tags_normalized_address", "address_risk_tags", ["normalized_address"])
    op.create_index("ix_address_risk_tags_tag", "address_risk_tags", ["tag"])
    op.create_index("ix_address_risk_tags_is_active", "address_risk_tags", ["is_active"])
    op.create_index("ix_address_risk_lookup", "address_risk_tags", ["network", "normalized_address", "is_active"])

    op.create_table(
        "risk_assessments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("network", sa.String(length=32), nullable=False),
        sa.Column("address", sa.String(length=128), nullable=False),
        sa.Column("normalized_address", sa.String(length=128), nullable=False),
        sa.Column("model_version", sa.String(length=64), nullable=True),
        sa.Column("raw_score", sa.Numeric(5, 2), nullable=False),
        sa.Column("final_score", sa.Numeric(5, 2), nullable=False),
        sa.Column("risk_zone", sa.String(length=32), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("override_hit", sa.Boolean(), nullable=False),
        sa.Column("override_reasons", postgresql.JSONB(), nullable=True),
        sa.Column("request_payload", postgresql.JSONB(), nullable=True),
        sa.Column("wallet_report", postgresql.JSONB(), nullable=True),
        sa.Column("trace_result", postgresql.JSONB(), nullable=True),
        sa.Column("sanctions_screening", postgresql.JSONB(), nullable=True),
        sa.Column("result_payload", postgresql.JSONB(), nullable=False),
        sa.Column("data_quality", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_risk_assessments_network", "risk_assessments", ["network"])
    op.create_index("ix_risk_assessments_normalized_address", "risk_assessments", ["normalized_address"])
    op.create_index("ix_risk_assessments_risk_zone", "risk_assessments", ["risk_zone"])
    op.create_index("ix_risk_assessments_override_hit", "risk_assessments", ["override_hit"])
    op.create_index("ix_risk_assessments_created_at", "risk_assessments", ["created_at"])
    op.create_index("ix_risk_assessment_address_created", "risk_assessments", ["network", "normalized_address", "created_at"])
    op.create_index("ix_risk_assessment_zone_created", "risk_assessments", ["risk_zone", "created_at"])

    op.create_table(
        "risk_assessment_evidence",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("assessment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_type", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=True),
        sa.Column("subject_type", sa.String(length=64), nullable=True),
        sa.Column("subject_value", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["assessment_id"], ["risk_assessments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_risk_assessment_evidence_evidence_type", "risk_assessment_evidence", ["evidence_type"])
    op.create_index("ix_risk_evidence_assessment", "risk_assessment_evidence", ["assessment_id"])
    op.create_index("ix_risk_evidence_type_created", "risk_assessment_evidence", ["evidence_type", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_risk_evidence_type_created", table_name="risk_assessment_evidence")
    op.drop_index("ix_risk_evidence_assessment", table_name="risk_assessment_evidence")
    op.drop_index("ix_risk_assessment_evidence_evidence_type", table_name="risk_assessment_evidence")
    op.drop_table("risk_assessment_evidence")

    op.drop_index("ix_risk_assessment_zone_created", table_name="risk_assessments")
    op.drop_index("ix_risk_assessment_address_created", table_name="risk_assessments")
    op.drop_index("ix_risk_assessments_created_at", table_name="risk_assessments")
    op.drop_index("ix_risk_assessments_override_hit", table_name="risk_assessments")
    op.drop_index("ix_risk_assessments_risk_zone", table_name="risk_assessments")
    op.drop_index("ix_risk_assessments_normalized_address", table_name="risk_assessments")
    op.drop_index("ix_risk_assessments_network", table_name="risk_assessments")
    op.drop_table("risk_assessments")

    op.drop_index("ix_address_risk_lookup", table_name="address_risk_tags")
    op.drop_index("ix_address_risk_tags_is_active", table_name="address_risk_tags")
    op.drop_index("ix_address_risk_tags_tag", table_name="address_risk_tags")
    op.drop_index("ix_address_risk_tags_normalized_address", table_name="address_risk_tags")
    op.drop_index("ix_address_risk_tags_network", table_name="address_risk_tags")
    op.drop_table("address_risk_tags")
