"""
app/workers/sync_worker.py

Incremental sync pipeline:
1. Load wallets with pending/stale sync_state
2. For each wallet → adapter.fetch_transactions(from_block=last_synced_block)
3. Upsert transactions (deduplicate via unique constraint)
4. Update sync_state
5. Trigger aggregation for affected months
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import select, update, and_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.core.config import get_settings
from app.adapters.adapters import get_adapter, NormalisedTx
from app.models import (
    Wallet, Asset, Transaction, SyncState, Network,
    TxDirectionEnum, SyncStatusEnum
)
from app.workers.aggregator import rebuild_monthly_aggregates

settings = get_settings()
logger = logging.getLogger(__name__)


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def get_or_create_asset(
    session: AsyncSession,
    network_id: int,
    symbol: str,
    contract: Optional[str],
    is_native: bool,
) -> int:
    result = await session.execute(
        select(Asset.id).where(
            and_(
                Asset.network_id == network_id,
                Asset.contract_address == contract,
            )
        )
    )
    row = result.scalar_one_or_none()
    if row:
        return row

    asset = Asset(
        network_id=network_id,
        contract_address=contract,
        symbol=symbol,
        name=symbol,
        decimals=18,
        is_native=is_native,
    )
    session.add(asset)
    await session.flush()
    return asset.id


async def upsert_transaction(
    session: AsyncSession,
    wallet_id: UUID,
    asset_id: int,
    ntx: NormalisedTx,
    wallet_address: str,
) -> bool:
    """Insert transaction, skip on conflict (deduplication). Returns True if new."""
    direction = (
        TxDirectionEnum.incoming
        if ntx.to_address.lower() == wallet_address.lower()
        else TxDirectionEnum.outgoing
    )

    stmt = pg_insert(Transaction).values(
        wallet_id=wallet_id,
        asset_id=asset_id,
        tx_hash=ntx.tx_hash,
        block_number=ntx.block_number,
        block_timestamp=ntx.block_timestamp,
        from_address=ntx.from_address,
        to_address=ntx.to_address,
        raw_amount=ntx.raw_amount,
        amount=ntx.amount,
        direction=direction,
        fee_amount=ntx.fee_amount,
        is_error=ntx.is_error,
        raw_data=ntx.raw_data,
    ).on_conflict_do_nothing(
        constraint="uq_tx_wallet_asset"
    )

    result = await session.execute(stmt)
    return result.rowcount > 0


# ─── Single-wallet sync ───────────────────────────────────────────────────────

async def sync_wallet(wallet_id: UUID, force: bool = False):
    """
    Full incremental sync for a single wallet across all its assets.
    """
    async with AsyncSessionLocal() as session:
        wallet = await session.get(Wallet, wallet_id)
        if not wallet or not wallet.is_active:
            return

        network = await session.get(Network, wallet.network_id)
        adapter = get_adapter(network.slug)

        # Load or create sync state
        result = await session.execute(
            select(SyncState).where(
                and_(
                    SyncState.wallet_id == wallet_id,
                    SyncState.asset_id == None,
                )
            )
        )
        sync_state = result.scalar_one_or_none()
        if not sync_state:
            sync_state = SyncState(wallet_id=wallet_id, asset_id=None)
            session.add(sync_state)
            await session.flush()

        if sync_state.status == SyncStatusEnum.running and not force:
            logger.info(f"Wallet {wallet_id} already syncing, skipping")
            return

        # Mark as running
        sync_state.status = SyncStatusEnum.running
        sync_state.error_message = None
        await session.commit()

        affected_months: set[tuple[int, int]] = set()

        try:
            count = 0
            async for ntx in adapter.fetch_transactions(
                address=wallet.address,
                from_block=sync_state.last_synced_block or 0,
                from_tx=sync_state.last_synced_tx,
            ):
                async with AsyncSessionLocal() as inner:
                    asset_id = await get_or_create_asset(
                        inner,
                        wallet.network_id,
                        ntx.asset_symbol,
                        ntx.asset_contract,
                        ntx.is_native,
                    )

                    is_new = await upsert_transaction(
                        inner, wallet_id, asset_id, ntx, wallet.address
                    )
                    if is_new:
                        count += 1
                        affected_months.add((
                            ntx.block_timestamp.year,
                            ntx.block_timestamp.month,
                        ))

                        # Advance sync cursor
                        if ntx.block_number and ntx.block_number > (sync_state.last_synced_block or 0):
                            sync_state.last_synced_block = ntx.block_number
                        sync_state.last_synced_tx = ntx.tx_hash

                    await inner.commit()

            # Update sync state
            async with AsyncSessionLocal() as final:
                result = await final.execute(
                    select(SyncState).where(SyncState.id == sync_state.id)
                )
                ss = result.scalar_one()
                ss.status = SyncStatusEnum.completed
                ss.last_synced_at = datetime.now(timezone.utc)
                ss.retry_count = 0
                await final.commit()

            logger.info(f"Wallet {wallet_id}: +{count} new txs, months={affected_months}")

            # Rebuild aggregates for affected months
            for year, month in affected_months:
                await rebuild_monthly_aggregates(wallet_id, year, month)

        except Exception as e:
            logger.error(f"Sync failed for wallet {wallet_id}: {e}", exc_info=True)
            async with AsyncSessionLocal() as err_session:
                result = await err_session.execute(
                    select(SyncState).where(SyncState.id == sync_state.id)
                )
                ss = result.scalar_one()
                ss.status = SyncStatusEnum.failed
                ss.error_message = str(e)
                ss.retry_count = (ss.retry_count or 0) + 1
                await err_session.commit()


# ─── Batch sync all wallets ───────────────────────────────────────────────────

async def sync_all_wallets(batch_size: int = settings.SYNC_BATCH_SIZE):
    """
    Distribute sync across all active wallets in parallel batches.
    Respects per-network rate limits via adapter limiters.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Wallet.id).where(Wallet.is_active == True)
        )
        wallet_ids = [row[0] for row in result.fetchall()]

    logger.info(f"Starting batch sync for {len(wallet_ids)} wallets")

    semaphore = asyncio.Semaphore(batch_size)

    async def bounded_sync(wid):
        async with semaphore:
            await sync_wallet(wid)

    tasks = [bounded_sync(wid) for wid in wallet_ids]
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Batch sync complete")
