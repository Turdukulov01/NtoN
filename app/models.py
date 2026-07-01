"""
SQLAlchemy ORM models — full schema
Tables: wallets, networks, assets, transactions, daily_snapshots,
        monthly_aggregates, sync_state, users, audit_log
"""
from datetime import datetime, date
from decimal import Decimal
from typing import Optional
from sqlalchemy import (
    Column, String, Integer, BigInteger, Numeric, Boolean,
    DateTime, Date, Text, ForeignKey, UniqueConstraint, Index,
    Enum as SAEnum
)
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import UUID, JSONB
import uuid
import enum

from app.core.database import Base


# ─── Enumerations ────────────────────────────────────────────────────────────

class NetworkEnum(str, enum.Enum):
    bitcoin = "bitcoin"
    ethereum = "ethereum"
    tron = "tron"


class TxDirectionEnum(str, enum.Enum):
    incoming = "incoming"
    outgoing = "outgoing"
    self_transfer = "self_transfer"


class SyncStatusEnum(str, enum.Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


class UserRoleEnum(str, enum.Enum):
    admin = "admin"
    analyst = "analyst"
    viewer = "viewer"


class SanctionsSourceEnum(str, enum.Enum):
    UK_OFSI = "UK_OFSI"
    UN = "UN"
    US_OFAC = "US_OFAC"
    EU = "EU"


class SanctionsSubjectTypeEnum(str, enum.Enum):
    individual = "individual"
    entity = "entity"
    ship = "ship"
    aircraft = "aircraft"
    unknown = "unknown"


class SanctionsNameTypeEnum(str, enum.Enum):
    primary = "primary"
    alias = "alias"
    variation = "variation"
    non_latin = "non_latin"


class SanctionsDocumentTypeEnum(str, enum.Enum):
    passport = "passport"
    national_id = "national_id"
    business_registration = "business_registration"
    imo = "imo"
    hin = "hin"
    other = "other"


class SanctionsMatchLevelEnum(str, enum.Enum):
    exact = "exact"
    strong = "strong"
    weak = "weak"


class SanctionsReviewStatusEnum(str, enum.Enum):
    pending = "pending"
    confirmed = "confirmed"
    false_positive = "false_positive"


# ─── Users ───────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    role = Column(SAEnum(UserRoleEnum), nullable=False, default=UserRoleEnum.viewer)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    last_login = Column(DateTime(timezone=True), nullable=True)


# ─── Networks ────────────────────────────────────────────────────────────────

class Network(Base):
    __tablename__ = "networks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(SAEnum(NetworkEnum), unique=True, nullable=False)
    display_name = Column(String(64), nullable=False)
    native_asset = Column(String(16), nullable=False)   # BTC / ETH / TRX
    decimals = Column(Integer, default=18)
    is_active = Column(Boolean, default=True)

    wallets = relationship("Wallet", back_populates="network")
    assets = relationship("Asset", back_populates="network")


# ─── Wallets ─────────────────────────────────────────────────────────────────

class Wallet(Base):
    __tablename__ = "wallets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    address = Column(String(128), nullable=False)
    network_id = Column(Integer, ForeignKey("networks.id"), nullable=False)
    label = Column(String(255), nullable=True)
    tags = Column(JSONB, default=list)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    network = relationship("Network", back_populates="wallets")
    transactions = relationship("Transaction", back_populates="wallet")
    sync_states = relationship("SyncState", back_populates="wallet")
    daily_snapshots = relationship("DailySnapshot", back_populates="wallet")
    monthly_aggregates = relationship("MonthlyAggregate", back_populates="wallet")

    __table_args__ = (
        UniqueConstraint("address", "network_id", name="uq_wallet_address_network"),
        Index("ix_wallet_address", "address"),
        Index("ix_wallet_network", "network_id"),
        Index("ix_wallet_active", "is_active"),
    )


# ─── Assets / Tokens ─────────────────────────────────────────────────────────

class Asset(Base):
    """ERC-20 / TRC-20 tokens and native coins"""
    __tablename__ = "assets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    network_id = Column(Integer, ForeignKey("networks.id"), nullable=False)
    contract_address = Column(String(128), nullable=True)   # None = native
    symbol = Column(String(32), nullable=False)
    name = Column(String(128), nullable=False)
    decimals = Column(Integer, default=18)
    is_native = Column(Boolean, default=False)
    coingecko_id = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    network = relationship("Network", back_populates="assets")

    __table_args__ = (
        UniqueConstraint("network_id", "contract_address", name="uq_asset_contract_network"),
        Index("ix_asset_symbol", "symbol"),
        Index("ix_asset_network", "network_id"),
    )


# ─── Transactions ─────────────────────────────────────────────────────────────

class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    wallet_id = Column(UUID(as_uuid=True), ForeignKey("wallets.id"), nullable=False)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False)
    tx_hash = Column(String(128), nullable=False)
    block_number = Column(BigInteger, nullable=True)
    block_timestamp = Column(DateTime(timezone=True), nullable=False)
    from_address = Column(String(128), nullable=False)
    to_address = Column(String(128), nullable=False)

    # Normalised amount in asset's smallest unit (satoshi/wei/sun)
    raw_amount = Column(Numeric(38, 0), nullable=False)
    # Human-readable amount (raw / 10^decimals)
    amount = Column(Numeric(36, 18), nullable=False)

    direction = Column(SAEnum(TxDirectionEnum), nullable=False)
    fee_amount = Column(Numeric(36, 18), nullable=True)
    fee_asset_id = Column(Integer, ForeignKey("assets.id"), nullable=True)

    # USD value at time of tx (optional, from price oracle)
    usd_value = Column(Numeric(20, 6), nullable=True)

    is_error = Column(Boolean, default=False)
    raw_data = Column(JSONB, nullable=True)   # provider raw payload
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    wallet = relationship("Wallet", back_populates="transactions")
    asset = relationship("Asset", foreign_keys=[asset_id])

    __table_args__ = (
        # Deduplication: same tx_hash + wallet + asset
        UniqueConstraint("tx_hash", "wallet_id", "asset_id", name="uq_tx_wallet_asset"),
        Index("ix_tx_wallet", "wallet_id"),
        Index("ix_tx_hash", "tx_hash"),
        Index("ix_tx_timestamp", "block_timestamp"),
        Index("ix_tx_wallet_ts", "wallet_id", "block_timestamp"),
        Index("ix_tx_asset", "asset_id"),
    )


# ─── Sync State ───────────────────────────────────────────────────────────────

class SyncState(Base):
    """
    Tracks incremental sync position per wallet × asset.
    last_synced_block / last_synced_tx prevent re-downloading history.
    """
    __tablename__ = "sync_states"

    id = Column(Integer, primary_key=True, autoincrement=True)
    wallet_id = Column(UUID(as_uuid=True), ForeignKey("wallets.id"), nullable=False)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=True)
    last_synced_block = Column(BigInteger, default=0)
    last_synced_tx = Column(String(128), nullable=True)
    last_synced_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(SAEnum(SyncStatusEnum), default=SyncStatusEnum.pending)
    error_message = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0)

    wallet = relationship("Wallet", back_populates="sync_states")

    __table_args__ = (
        UniqueConstraint("wallet_id", "asset_id", name="uq_sync_wallet_asset"),
        Index("ix_sync_status", "status"),
        Index("ix_sync_wallet", "wallet_id"),
    )


# ─── Daily Snapshots ──────────────────────────────────────────────────────────

class DailySnapshot(Base):
    """
    End-of-day balance per wallet × asset.
    Rebuilt each sync cycle.
    """
    __tablename__ = "daily_snapshots"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    wallet_id = Column(UUID(as_uuid=True), ForeignKey("wallets.id"), nullable=False)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False)
    snapshot_date = Column(Date, nullable=False)
    closing_balance = Column(Numeric(36, 18), nullable=False, default=0)
    closing_balance_usd = Column(Numeric(20, 6), nullable=True)

    wallet = relationship("Wallet", back_populates="daily_snapshots")

    __table_args__ = (
        UniqueConstraint("wallet_id", "asset_id", "snapshot_date", name="uq_snapshot"),
        Index("ix_snapshot_date", "snapshot_date"),
        Index("ix_snapshot_wallet_date", "wallet_id", "snapshot_date"),
    )


# ─── Monthly Aggregates ───────────────────────────────────────────────────────

class MonthlyAggregate(Base):
    """
    Volume metrics per wallet × asset × month.

    FORMULAS (industry standard):
    ─────────────────────────────
    volume_in    = Σ amount  where direction = incoming AND NOT is_error
    volume_out   = Σ amount  where direction = outgoing AND NOT is_error
    volume_total = volume_in + volume_out   (turnover / gross volume)
    net_flow     = volume_in − volume_out   (positive = net receiver)
    tx_count_in  = count(direction = incoming)
    tx_count_out = count(direction = outgoing)
    """
    __tablename__ = "monthly_aggregates"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    wallet_id = Column(UUID(as_uuid=True), ForeignKey("wallets.id"), nullable=False)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False)
    year = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)          # 1–12

    volume_in = Column(Numeric(36, 18), default=0)
    volume_out = Column(Numeric(36, 18), default=0)
    volume_total = Column(Numeric(36, 18), default=0)
    net_flow = Column(Numeric(36, 18), default=0)
    tx_count_in = Column(Integer, default=0)
    tx_count_out = Column(Integer, default=0)
    tx_count_total = Column(Integer, default=0)

    # USD-denominated (avg exchange rate for month)
    volume_in_usd = Column(Numeric(20, 6), nullable=True)
    volume_out_usd = Column(Numeric(20, 6), nullable=True)
    volume_total_usd = Column(Numeric(20, 6), nullable=True)
    net_flow_usd = Column(Numeric(20, 6), nullable=True)

    computed_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    wallet = relationship("Wallet", back_populates="monthly_aggregates")

    __table_args__ = (
        UniqueConstraint("wallet_id", "asset_id", "year", "month", name="uq_monthly"),
        Index("ix_monthly_wallet", "wallet_id"),
        Index("ix_monthly_period", "year", "month"),
    )


# ─── Audit Log ────────────────────────────────────────────────────────────────

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    action = Column(String(128), nullable=False)
    resource_type = Column(String(64), nullable=True)
    resource_id = Column(String(128), nullable=True)
    details = Column(JSONB, nullable=True)
    ip_address = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        Index("ix_audit_user", "user_id"),
        Index("ix_audit_created", "created_at"),
        Index("ix_audit_action", "action"),
    )


# ─── Sanctions Screening ─────────────────────────────────────────────────────

class SanctionsImportRun(Base):
    __tablename__ = "sanctions_import_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source = Column(SAEnum(SanctionsSourceEnum), nullable=False, index=True)
    list_name = Column(String(128), nullable=False)
    source_url = Column(Text, nullable=True)
    source_format = Column(String(32), nullable=False)
    source_sha256 = Column(String(64), nullable=False)
    publication_date = Column(Date, nullable=True)
    parser_version = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False, default="running")
    record_count = Column(Integer, nullable=False, default=0)
    metadata_json = Column(JSONB, nullable=True)
    started_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    subjects = relationship("SanctionsSubject", back_populates="import_run")

    __table_args__ = (
        Index("ix_sanctions_import_source_started", "source", "started_at"),
        Index("ix_sanctions_import_sha", "source_sha256"),
    )


class SanctionsSubject(Base):
    __tablename__ = "sanctions_subjects"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    import_run_id = Column(UUID(as_uuid=True), ForeignKey("sanctions_import_runs.id"), nullable=True)
    source = Column(SAEnum(SanctionsSourceEnum), nullable=False, index=True)
    list_name = Column(String(128), nullable=False)
    subject_type = Column(SAEnum(SanctionsSubjectTypeEnum), nullable=False, index=True)
    primary_name = Column(String(512), nullable=False)
    primary_name_key = Column(String(512), nullable=False, index=True)
    program = Column(String(512), nullable=False, index=True)
    sanctions_imposed = Column(JSONB, default=list)
    designation_source = Column(String(64), nullable=True)
    date_designated = Column(Date, nullable=True)
    last_updated = Column(Date, nullable=True)
    unique_id = Column(String(64), nullable=False)
    ofsi_group_id = Column(String(64), nullable=True, index=True)
    un_reference_id = Column(String(64), nullable=True, index=True)
    raw_text = Column(Text, nullable=False)
    source_payload = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    import_run = relationship("SanctionsImportRun", back_populates="subjects")
    names = relationship("SanctionsName", back_populates="subject", cascade="all, delete-orphan")
    documents = relationship("SanctionsDocument", back_populates="subject", cascade="all, delete-orphan")
    addresses = relationship("SanctionsAddress", back_populates="subject", cascade="all, delete-orphan")
    matches = relationship("SanctionsMatch", back_populates="subject")

    __table_args__ = (
        UniqueConstraint("source", "unique_id", name="uq_sanctions_subject_source_unique"),
        Index("ix_sanctions_subject_name_type", "primary_name_key", "subject_type"),
        Index("ix_sanctions_subject_program", "source", "program"),
    )


class SanctionsName(Base):
    __tablename__ = "sanctions_names"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    subject_id = Column(UUID(as_uuid=True), ForeignKey("sanctions_subjects.id", ondelete="CASCADE"), nullable=False)
    name_type = Column(SAEnum(SanctionsNameTypeEnum), nullable=False)
    value = Column(String(1024), nullable=False)
    normalized_value = Column(String(1024), nullable=False, index=True)
    quality = Column(String(64), nullable=True)
    script = Column(String(64), nullable=True)
    language = Column(String(64), nullable=True)

    subject = relationship("SanctionsSubject", back_populates="names")

    __table_args__ = (
        Index("ix_sanctions_names_subject_type", "subject_id", "name_type"),
        Index("ix_sanctions_names_norm_type", "normalized_value", "name_type"),
    )


class SanctionsDocument(Base):
    __tablename__ = "sanctions_documents"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    subject_id = Column(UUID(as_uuid=True), ForeignKey("sanctions_subjects.id", ondelete="CASCADE"), nullable=False)
    document_type = Column(SAEnum(SanctionsDocumentTypeEnum), nullable=False)
    value = Column(String(255), nullable=False)
    normalized_value = Column(String(255), nullable=False, index=True)
    country = Column(String(128), nullable=True)
    note = Column(Text, nullable=True)

    subject = relationship("SanctionsSubject", back_populates="documents")

    __table_args__ = (
        Index("ix_sanctions_docs_type_value", "document_type", "normalized_value"),
        Index("ix_sanctions_docs_subject", "subject_id"),
    )


class SanctionsAddress(Base):
    __tablename__ = "sanctions_addresses"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    subject_id = Column(UUID(as_uuid=True), ForeignKey("sanctions_subjects.id", ondelete="CASCADE"), nullable=False)
    full_text = Column(Text, nullable=False)
    normalized_text = Column(Text, nullable=False)
    country = Column(String(128), nullable=True)
    parts = Column(JSONB, nullable=True)

    subject = relationship("SanctionsSubject", back_populates="addresses")

    __table_args__ = (
        Index("ix_sanctions_addr_subject", "subject_id"),
    )


class SanctionsMatch(Base):
    __tablename__ = "sanctions_matches"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    checked_subject_type = Column(String(64), nullable=False, index=True)
    checked_subject_value = Column(Text, nullable=False)
    normalized_checked_value = Column(Text, nullable=False)
    sanctions_subject_id = Column(UUID(as_uuid=True), ForeignKey("sanctions_subjects.id"), nullable=False)
    match_score = Column(Numeric(5, 2), nullable=False)
    match_level = Column(SAEnum(SanctionsMatchLevelEnum), nullable=False)
    matched_fields = Column(JSONB, default=list)
    override_hit = Column(Boolean, nullable=False, default=False, index=True)
    review_status = Column(SAEnum(SanctionsReviewStatusEnum), nullable=False, default=SanctionsReviewStatusEnum.pending)
    evidence = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    subject = relationship("SanctionsSubject", back_populates="matches")

    __table_args__ = (
        Index("ix_sanctions_matches_subject", "sanctions_subject_id"),
        Index("ix_sanctions_matches_review", "review_status", "override_hit"),
    )


# ─── Risk Assessment ────────────────────────────────────────────────────────

class AddressRiskTag(Base):
    __tablename__ = "address_risk_tags"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    network = Column(String(32), nullable=False, index=True)
    address = Column(String(128), nullable=False)
    normalized_address = Column(String(128), nullable=False, index=True)
    tag = Column(String(128), nullable=False, index=True)
    label = Column(String(255), nullable=True)
    source = Column(String(128), nullable=False, default="manual")
    confidence = Column(Numeric(5, 2), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    note = Column(Text, nullable=True)
    metadata_json = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    events = relationship("AddressRiskTagEvent", back_populates="address_risk_tag", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("network", "normalized_address", "tag", "source", name="uq_address_risk_tag_source"),
        Index("ix_address_risk_lookup", "network", "normalized_address", "is_active"),
    )


class AddressRiskTagEvent(Base):
    __tablename__ = "address_risk_tag_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    address_risk_tag_id = Column(UUID(as_uuid=True), ForeignKey("address_risk_tags.id", ondelete="CASCADE"), nullable=False)
    action = Column(String(32), nullable=False, index=True)
    actor = Column(String(128), nullable=True)
    previous_payload = Column(JSONB, nullable=True)
    new_payload = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    address_risk_tag = relationship("AddressRiskTag", back_populates="events")

    __table_args__ = (
        Index("ix_address_risk_tag_events_tag", "address_risk_tag_id", "created_at"),
        Index("ix_address_risk_tag_events_action_created", "action", "created_at"),
    )


class KytProviderReport(Base):
    __tablename__ = "kyt_provider_reports"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider = Column(String(64), nullable=False, index=True)
    network = Column(String(32), nullable=False, index=True)
    address = Column(String(128), nullable=False)
    normalized_address = Column(String(128), nullable=False, index=True)
    provider_score = Column(Numeric(7, 2), nullable=True)
    provider_risk_level = Column(String(64), nullable=True)
    tx_total = Column(BigInteger, nullable=True)
    activity_first = Column(DateTime(timezone=True), nullable=True)
    activity_last = Column(DateTime(timezone=True), nullable=True)
    raw_response = Column(JSONB, nullable=False)
    normalized_payload = Column(JSONB, nullable=False)
    fetched_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    exposures = relationship("KytExposure", back_populates="report", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("provider", "network", "normalized_address", name="uq_kyt_provider_report_subject"),
        Index("ix_kyt_report_lookup", "provider", "network", "normalized_address", "expires_at"),
        Index("ix_kyt_report_address_fetched", "network", "normalized_address", "fetched_at"),
    )


class KytExposure(Base):
    __tablename__ = "kyt_exposures"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    report_id = Column(UUID(as_uuid=True), ForeignKey("kyt_provider_reports.id", ondelete="CASCADE"), nullable=False)
    provider = Column(String(64), nullable=False, index=True)
    network = Column(String(32), nullable=False, index=True)
    address = Column(String(128), nullable=False)
    normalized_address = Column(String(128), nullable=False, index=True)
    category_name = Column(String(255), nullable=False)
    category_key = Column(String(255), nullable=False, index=True)
    exposure_group = Column(String(64), nullable=True, index=True)
    percent = Column(Numeric(9, 4), nullable=False)
    amount = Column(Text, nullable=True)
    amount_human = Column(String(128), nullable=True)
    provider_risk_score = Column(Numeric(9, 4), nullable=True)
    model_category_score = Column(Numeric(9, 4), nullable=True)
    model_contribution_score = Column(Numeric(9, 4), nullable=True)
    payload = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    report = relationship("KytProviderReport", back_populates="exposures")

    __table_args__ = (
        Index("ix_kyt_exposure_report", "report_id"),
        Index("ix_kyt_exposure_lookup", "provider", "network", "normalized_address", "category_key"),
        Index("ix_kyt_exposure_group_percent", "exposure_group", "percent"),
    )


class RiskAssessment(Base):
    __tablename__ = "risk_assessments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    network = Column(String(32), nullable=False, index=True)
    address = Column(String(128), nullable=False)
    normalized_address = Column(String(128), nullable=False, index=True)
    model_version = Column(String(64), nullable=True)
    raw_score = Column(Numeric(5, 2), nullable=False)
    final_score = Column(Numeric(5, 2), nullable=False)
    risk_zone = Column(String(32), nullable=False, index=True)
    category = Column(String(64), nullable=True)
    override_hit = Column(Boolean, nullable=False, default=False, index=True)
    override_reasons = Column(JSONB, nullable=True)
    request_payload = Column(JSONB, nullable=True)
    wallet_report = Column(JSONB, nullable=True)
    trace_result = Column(JSONB, nullable=True)
    sanctions_screening = Column(JSONB, nullable=True)
    result_payload = Column(JSONB, nullable=False)
    data_quality = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False, index=True)

    evidence = relationship("RiskAssessmentEvidence", back_populates="assessment", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_risk_assessment_address_created", "network", "normalized_address", "created_at"),
        Index("ix_risk_assessment_zone_created", "risk_zone", "created_at"),
    )


class RiskAssessmentEvidence(Base):
    __tablename__ = "risk_assessment_evidence"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    assessment_id = Column(UUID(as_uuid=True), ForeignKey("risk_assessments.id", ondelete="CASCADE"), nullable=False)
    evidence_type = Column(String(64), nullable=False, index=True)
    source = Column(String(128), nullable=True)
    subject_type = Column(String(64), nullable=True)
    subject_value = Column(Text, nullable=True)
    payload = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    assessment = relationship("RiskAssessment", back_populates="evidence")

    __table_args__ = (
        Index("ix_risk_evidence_assessment", "assessment_id"),
        Index("ix_risk_evidence_type_created", "evidence_type", "created_at"),
    )
