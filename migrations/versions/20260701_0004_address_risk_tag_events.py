"""Add address risk tag event history.

Revision ID: 20260701_0004
Revises: 20260701_0003
Create Date: 2026-07-01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260701_0004"
down_revision = "20260701_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "address_risk_tag_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("address_risk_tag_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=True),
        sa.Column("previous_payload", postgresql.JSONB(), nullable=True),
        sa.Column("new_payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["address_risk_tag_id"], ["address_risk_tags.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_address_risk_tag_events_action", "address_risk_tag_events", ["action"])
    op.create_index(
        "ix_address_risk_tag_events_tag",
        "address_risk_tag_events",
        ["address_risk_tag_id", "created_at"],
    )
    op.create_index(
        "ix_address_risk_tag_events_action_created",
        "address_risk_tag_events",
        ["action", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_address_risk_tag_events_action_created", table_name="address_risk_tag_events")
    op.drop_index("ix_address_risk_tag_events_tag", table_name="address_risk_tag_events")
    op.drop_index("ix_address_risk_tag_events_action", table_name="address_risk_tag_events")
    op.drop_table("address_risk_tag_events")
