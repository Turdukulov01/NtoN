"""Initial database schema.

Revision ID: 20260630_0001
Revises:
Create Date: 2026-06-30
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260630_0001"
down_revision = None
branch_labels = None
depends_on = None


metadata = sa.MetaData()

network_enum = sa.Enum("bitcoin", "ethereum", "tron", name="networkenum")
tx_direction_enum = sa.Enum("incoming", "outgoing", "self_transfer", name="txdirectionenum")
sync_status_enum = sa.Enum("pending", "running", "completed", "failed", name="syncstatusenum")
user_role_enum = sa.Enum("admin", "analyst", "viewer", name="userroleenum")
sanctions_source_enum = sa.Enum("UK_OFSI", "UN", "US_OFAC", "EU", name="sanctionssourceenum")
sanctions_subject_type_enum = sa.Enum(
    "individual",
    "entity",
    "ship",
    "aircraft",
    "unknown",
    name="sanctionssubjecttypeenum",
)
sanctions_name_type_enum = sa.Enum("primary", "alias", "variation", "non_latin", name="sanctionsnametypeenum")
sanctions_document_type_enum = sa.Enum(
    "passport",
    "national_id",
    "business_registration",
    "imo",
    "hin",
    "other",
    name="sanctionsdocumenttypeenum",
)
sanctions_match_level_enum = sa.Enum("exact", "strong", "weak", name="sanctionsmatchlevelenum")
sanctions_review_status_enum = sa.Enum(
    "pending",
    "confirmed",
    "false_positive",
    name="sanctionsreviewstatusenum",
)


sa.Table(
    "users",
    metadata,
    sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("email", sa.String(255), nullable=False, unique=True, index=True),
    sa.Column("hashed_password", sa.String(255), nullable=False),
    sa.Column("role", user_role_enum, nullable=False),
    sa.Column("is_active", sa.Boolean(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True)),
    sa.Column("last_login", sa.DateTime(timezone=True)),
)

sa.Table(
    "networks",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("slug", network_enum, nullable=False, unique=True),
    sa.Column("display_name", sa.String(64), nullable=False),
    sa.Column("native_asset", sa.String(16), nullable=False),
    sa.Column("decimals", sa.Integer()),
    sa.Column("is_active", sa.Boolean()),
)

sa.Table(
    "wallets",
    metadata,
    sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("address", sa.String(128), nullable=False),
    sa.Column("network_id", sa.Integer(), sa.ForeignKey("networks.id"), nullable=False),
    sa.Column("label", sa.String(255)),
    sa.Column("tags", postgresql.JSONB()),
    sa.Column("is_active", sa.Boolean()),
    sa.Column("created_at", sa.DateTime(timezone=True)),
    sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
    sa.UniqueConstraint("address", "network_id", name="uq_wallet_address_network"),
    sa.Index("ix_wallet_address", "address"),
    sa.Index("ix_wallet_network", "network_id"),
    sa.Index("ix_wallet_active", "is_active"),
)

sa.Table(
    "assets",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("network_id", sa.Integer(), sa.ForeignKey("networks.id"), nullable=False),
    sa.Column("contract_address", sa.String(128)),
    sa.Column("symbol", sa.String(32), nullable=False),
    sa.Column("name", sa.String(128), nullable=False),
    sa.Column("decimals", sa.Integer()),
    sa.Column("is_native", sa.Boolean()),
    sa.Column("coingecko_id", sa.String(128)),
    sa.Column("created_at", sa.DateTime(timezone=True)),
    sa.UniqueConstraint("network_id", "contract_address", name="uq_asset_contract_network"),
    sa.Index("ix_asset_symbol", "symbol"),
    sa.Index("ix_asset_network", "network_id"),
)

sa.Table(
    "transactions",
    metadata,
    sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("wallet_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("wallets.id"), nullable=False),
    sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
    sa.Column("tx_hash", sa.String(128), nullable=False),
    sa.Column("block_number", sa.BigInteger()),
    sa.Column("block_timestamp", sa.DateTime(timezone=True), nullable=False),
    sa.Column("from_address", sa.String(128), nullable=False),
    sa.Column("to_address", sa.String(128), nullable=False),
    sa.Column("raw_amount", sa.Numeric(38, 0), nullable=False),
    sa.Column("amount", sa.Numeric(36, 18), nullable=False),
    sa.Column("direction", tx_direction_enum, nullable=False),
    sa.Column("fee_amount", sa.Numeric(36, 18)),
    sa.Column("fee_asset_id", sa.Integer(), sa.ForeignKey("assets.id")),
    sa.Column("usd_value", sa.Numeric(20, 6)),
    sa.Column("is_error", sa.Boolean()),
    sa.Column("raw_data", postgresql.JSONB()),
    sa.Column("created_at", sa.DateTime(timezone=True)),
    sa.UniqueConstraint("tx_hash", "wallet_id", "asset_id", name="uq_tx_wallet_asset"),
    sa.Index("ix_tx_wallet", "wallet_id"),
    sa.Index("ix_tx_hash", "tx_hash"),
    sa.Index("ix_tx_timestamp", "block_timestamp"),
    sa.Index("ix_tx_wallet_ts", "wallet_id", "block_timestamp"),
    sa.Index("ix_tx_asset", "asset_id"),
)

sa.Table(
    "sync_states",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("wallet_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("wallets.id"), nullable=False),
    sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id")),
    sa.Column("last_synced_block", sa.BigInteger()),
    sa.Column("last_synced_tx", sa.String(128)),
    sa.Column("last_synced_at", sa.DateTime(timezone=True)),
    sa.Column("status", sync_status_enum),
    sa.Column("error_message", sa.Text()),
    sa.Column("retry_count", sa.Integer()),
    sa.UniqueConstraint("wallet_id", "asset_id", name="uq_sync_wallet_asset"),
    sa.Index("ix_sync_status", "status"),
    sa.Index("ix_sync_wallet", "wallet_id"),
)

sa.Table(
    "daily_snapshots",
    metadata,
    sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
    sa.Column("wallet_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("wallets.id"), nullable=False),
    sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
    sa.Column("snapshot_date", sa.Date(), nullable=False),
    sa.Column("closing_balance", sa.Numeric(36, 18), nullable=False),
    sa.Column("closing_balance_usd", sa.Numeric(20, 6)),
    sa.UniqueConstraint("wallet_id", "asset_id", "snapshot_date", name="uq_snapshot"),
    sa.Index("ix_snapshot_date", "snapshot_date"),
    sa.Index("ix_snapshot_wallet_date", "wallet_id", "snapshot_date"),
)

sa.Table(
    "monthly_aggregates",
    metadata,
    sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
    sa.Column("wallet_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("wallets.id"), nullable=False),
    sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
    sa.Column("year", sa.Integer(), nullable=False),
    sa.Column("month", sa.Integer(), nullable=False),
    sa.Column("volume_in", sa.Numeric(36, 18)),
    sa.Column("volume_out", sa.Numeric(36, 18)),
    sa.Column("volume_total", sa.Numeric(36, 18)),
    sa.Column("net_flow", sa.Numeric(36, 18)),
    sa.Column("tx_count_in", sa.Integer()),
    sa.Column("tx_count_out", sa.Integer()),
    sa.Column("tx_count_total", sa.Integer()),
    sa.Column("volume_in_usd", sa.Numeric(20, 6)),
    sa.Column("volume_out_usd", sa.Numeric(20, 6)),
    sa.Column("volume_total_usd", sa.Numeric(20, 6)),
    sa.Column("net_flow_usd", sa.Numeric(20, 6)),
    sa.Column("computed_at", sa.DateTime(timezone=True)),
    sa.UniqueConstraint("wallet_id", "asset_id", "year", "month", name="uq_monthly"),
    sa.Index("ix_monthly_wallet", "wallet_id"),
    sa.Index("ix_monthly_period", "year", "month"),
)

sa.Table(
    "audit_logs",
    metadata,
    sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
    sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
    sa.Column("action", sa.String(128), nullable=False),
    sa.Column("resource_type", sa.String(64)),
    sa.Column("resource_id", sa.String(128)),
    sa.Column("details", postgresql.JSONB()),
    sa.Column("ip_address", sa.String(64)),
    sa.Column("created_at", sa.DateTime(timezone=True)),
    sa.Index("ix_audit_user", "user_id"),
    sa.Index("ix_audit_created", "created_at"),
    sa.Index("ix_audit_action", "action"),
)

sa.Table(
    "sanctions_import_runs",
    metadata,
    sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("source", sanctions_source_enum, nullable=False, index=True),
    sa.Column("list_name", sa.String(128), nullable=False),
    sa.Column("source_url", sa.Text()),
    sa.Column("source_format", sa.String(32), nullable=False),
    sa.Column("source_sha256", sa.String(64), nullable=False),
    sa.Column("publication_date", sa.Date()),
    sa.Column("parser_version", sa.String(32), nullable=False),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("record_count", sa.Integer(), nullable=False),
    sa.Column("metadata_json", postgresql.JSONB()),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("completed_at", sa.DateTime(timezone=True)),
    sa.Index("ix_sanctions_import_source_started", "source", "started_at"),
    sa.Index("ix_sanctions_import_sha", "source_sha256"),
)

sa.Table(
    "sanctions_subjects",
    metadata,
    sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("import_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sanctions_import_runs.id")),
    sa.Column("source", sanctions_source_enum, nullable=False, index=True),
    sa.Column("list_name", sa.String(128), nullable=False),
    sa.Column("subject_type", sanctions_subject_type_enum, nullable=False, index=True),
    sa.Column("primary_name", sa.String(512), nullable=False),
    sa.Column("primary_name_key", sa.String(512), nullable=False, index=True),
    sa.Column("program", sa.String(512), nullable=False, index=True),
    sa.Column("sanctions_imposed", postgresql.JSONB()),
    sa.Column("designation_source", sa.String(64)),
    sa.Column("date_designated", sa.Date()),
    sa.Column("last_updated", sa.Date()),
    sa.Column("unique_id", sa.String(64), nullable=False),
    sa.Column("ofsi_group_id", sa.String(64), index=True),
    sa.Column("un_reference_id", sa.String(64), index=True),
    sa.Column("raw_text", sa.Text(), nullable=False),
    sa.Column("source_payload", postgresql.JSONB()),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("source", "unique_id", name="uq_sanctions_subject_source_unique"),
    sa.Index("ix_sanctions_subject_name_type", "primary_name_key", "subject_type"),
    sa.Index("ix_sanctions_subject_program", "source", "program"),
)

sa.Table(
    "sanctions_names",
    metadata,
    sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
    sa.Column(
        "subject_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("sanctions_subjects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("name_type", sanctions_name_type_enum, nullable=False),
    sa.Column("value", sa.String(1024), nullable=False),
    sa.Column("normalized_value", sa.String(1024), nullable=False, index=True),
    sa.Column("quality", sa.String(64)),
    sa.Column("script", sa.String(64)),
    sa.Column("language", sa.String(64)),
    sa.Index("ix_sanctions_names_subject_type", "subject_id", "name_type"),
    sa.Index("ix_sanctions_names_norm_type", "normalized_value", "name_type"),
)

sa.Table(
    "sanctions_documents",
    metadata,
    sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
    sa.Column(
        "subject_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("sanctions_subjects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("document_type", sanctions_document_type_enum, nullable=False),
    sa.Column("value", sa.String(255), nullable=False),
    sa.Column("normalized_value", sa.String(255), nullable=False, index=True),
    sa.Column("country", sa.String(128)),
    sa.Column("note", sa.Text()),
    sa.Index("ix_sanctions_docs_type_value", "document_type", "normalized_value"),
    sa.Index("ix_sanctions_docs_subject", "subject_id"),
)

sa.Table(
    "sanctions_addresses",
    metadata,
    sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
    sa.Column(
        "subject_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("sanctions_subjects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("full_text", sa.Text(), nullable=False),
    sa.Column("normalized_text", sa.Text(), nullable=False),
    sa.Column("country", sa.String(128)),
    sa.Column("parts", postgresql.JSONB()),
    sa.Index("ix_sanctions_addr_subject", "subject_id"),
)

sa.Table(
    "sanctions_matches",
    metadata,
    sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("checked_subject_type", sa.String(64), nullable=False, index=True),
    sa.Column("checked_subject_value", sa.Text(), nullable=False),
    sa.Column("normalized_checked_value", sa.Text(), nullable=False),
    sa.Column("sanctions_subject_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sanctions_subjects.id"), nullable=False),
    sa.Column("match_score", sa.Numeric(5, 2), nullable=False),
    sa.Column("match_level", sanctions_match_level_enum, nullable=False),
    sa.Column("matched_fields", postgresql.JSONB()),
    sa.Column("override_hit", sa.Boolean(), nullable=False, index=True),
    sa.Column("review_status", sanctions_review_status_enum, nullable=False),
    sa.Column("evidence", postgresql.JSONB()),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index("ix_sanctions_matches_subject", "sanctions_subject_id"),
    sa.Index("ix_sanctions_matches_review", "review_status", "override_hit"),
)


def upgrade() -> None:
    metadata.create_all(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    metadata.drop_all(op.get_bind(), checkfirst=True)
    for enum_name in (
        "sanctionsreviewstatusenum",
        "sanctionsmatchlevelenum",
        "sanctionsdocumenttypeenum",
        "sanctionsnametypeenum",
        "sanctionssubjecttypeenum",
        "sanctionssourceenum",
        "userroleenum",
        "syncstatusenum",
        "txdirectionenum",
        "networkenum",
    ):
        op.execute(sa.text(f"DROP TYPE IF EXISTS {enum_name}"))
