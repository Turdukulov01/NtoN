"""Deduplicate KYT provider reports.

Revision ID: 20260701_0005
Revises: 20260701_0004
Create Date: 2026-07-01
"""

from __future__ import annotations

from alembic import op


revision = "20260701_0005"
down_revision = "20260701_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM kyt_provider_reports stale
        USING kyt_provider_reports fresh
        WHERE stale.provider = fresh.provider
          AND stale.network = fresh.network
          AND stale.normalized_address = fresh.normalized_address
          AND (
            stale.fetched_at < fresh.fetched_at
            OR (
              stale.fetched_at = fresh.fetched_at
              AND stale.id::text < fresh.id::text
            )
          )
        """
    )
    op.create_unique_constraint(
        "uq_kyt_provider_report_subject",
        "kyt_provider_reports",
        ["provider", "network", "normalized_address"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_kyt_provider_report_subject",
        "kyt_provider_reports",
        type_="unique",
    )
