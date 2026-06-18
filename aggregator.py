"""
app/workers/aggregator.py

Computes MonthlyAggregate and DailySnapshot from raw transactions.

Formulas (industry standard):
  volume_in    = Σ amount WHERE direction=incoming AND NOT is_error
  volume_out   = Σ amount WHERE direction=outgoing AND NOT is_error
  volume_total = volume_in + volume_out   (gross turnover)
  net_flow     = volume_in − volume_out   (positive = net receiver)
"""
from __future__ import annotations
import logging
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID
from calendar import monthrange

from sqlalchemy import select, func, and_, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.database import AsyncSessionLocal
from app.models import (
    Transaction, MonthlyAggregate, DailySnapshot,
    TxDirectionEnum
)

logger = logging.getLogger(__name__)


async def rebuild_monthly_aggregates(wallet_id: UUID, year: int, month: int):
    """
    Recompute monthly aggregates for a wallet × month.
    Called after sync; safe to re-run (upsert).
    """
    async with AsyncSessionLocal() as session:
        # Group by asset_id + direction
        rows = await session.execute(
            select(
                Transaction.asset_id,
                Transaction.direction,
                func.count().label("cnt"),
                func.sum(Transaction.amount).label("total"),
                func.sum(Transaction.usd_value).label("total_usd"),
            ).where(
                and_(
                    Transaction.wallet_id == wallet_id,
                    func.extract("year", Transaction.block_timestamp) == year,
                    func.extract("month", Transaction.block_timestamp) == month,
                    Transaction.is_error == False,
                )
            ).group_by(Transaction.asset_id, Transaction.direction)
        )

        # Aggregate per asset
        by_asset: dict[int, dict] = {}
        for row in rows.fetchall():
            asset_id, direction, cnt, total, total_usd = row
            if asset_id not in by_asset:
                by_asset[asset_id] = {
                    "volume_in": Decimal(0),
                    "volume_out": Decimal(0),
                    "tx_count_in": 0,
                    "tx_count_out": 0,
                    "volume_in_usd": None,
                    "volume_out_usd": None,
                }
            d = by_asset[asset_id]
            if direction == TxDirectionEnum.incoming:
                d["volume_in"] = total or Decimal(0)
                d["tx_count_in"] = cnt
                d["volume_in_usd"] = total_usd
            elif direction == TxDirectionEnum.outgoing:
                d["volume_out"] = total or Decimal(0)
                d["tx_count_out"] = cnt
                d["volume_out_usd"] = total_usd

        for asset_id, d in by_asset.items():
            vin = d["volume_in"]
            vout = d["volume_out"]
            vin_usd = d["volume_in_usd"]
            vout_usd = d["volume_out_usd"]

            stmt = pg_insert(MonthlyAggregate).values(
                wallet_id=wallet_id,
                asset_id=asset_id,
                year=year,
                month=month,
                volume_in=vin,
                volume_out=vout,
                volume_total=vin + vout,
                net_flow=vin - vout,
                tx_count_in=d["tx_count_in"],
                tx_count_out=d["tx_count_out"],
                tx_count_total=d["tx_count_in"] + d["tx_count_out"],
                volume_in_usd=vin_usd,
                volume_out_usd=vout_usd,
                volume_total_usd=(vin_usd or 0) + (vout_usd or 0) if vin_usd or vout_usd else None,
                net_flow_usd=(vin_usd or 0) - (vout_usd or 0) if vin_usd or vout_usd else None,
            ).on_conflict_do_update(
                constraint="uq_monthly",
                set_={
                    "volume_in": vin,
                    "volume_out": vout,
                    "volume_total": vin + vout,
                    "net_flow": vin - vout,
                    "tx_count_in": d["tx_count_in"],
                    "tx_count_out": d["tx_count_out"],
                    "tx_count_total": d["tx_count_in"] + d["tx_count_out"],
                    "volume_in_usd": vin_usd,
                    "volume_out_usd": vout_usd,
                    "volume_total_usd": (vin_usd or 0) + (vout_usd or 0) if vin_usd or vout_usd else None,
                    "net_flow_usd": (vin_usd or 0) - (vout_usd or 0) if vin_usd or vout_usd else None,
                }
            )
            await session.execute(stmt)

        await session.commit()
        logger.info(f"Rebuilt monthly aggregates: wallet={wallet_id} {year}-{month:02d}")


async def rebuild_daily_snapshots(wallet_id: UUID, target_date: date):
    """
    Compute end-of-day balance for each asset on target_date.
    Balance = sum of all amounts (incoming +, outgoing −) up to and including target_date.
    """
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(
                Transaction.asset_id,
                func.sum(
                    func.case(
                        (Transaction.direction == TxDirectionEnum.incoming, Transaction.amount),
                        else_=-Transaction.amount
                    )
                ).label("balance"),
            ).where(
                and_(
                    Transaction.wallet_id == wallet_id,
                    func.date(Transaction.block_timestamp) <= target_date,
                    Transaction.is_error == False,
                )
            ).group_by(Transaction.asset_id)
        )

        for asset_id, balance in rows.fetchall():
            stmt = pg_insert(DailySnapshot).values(
                wallet_id=wallet_id,
                asset_id=asset_id,
                snapshot_date=target_date,
                closing_balance=balance or Decimal(0),
            ).on_conflict_do_update(
                constraint="uq_snapshot",
                set_={"closing_balance": balance or Decimal(0)}
            )
            await session.execute(stmt)

        await session.commit()
