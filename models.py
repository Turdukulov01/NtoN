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
