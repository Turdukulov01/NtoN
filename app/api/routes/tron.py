from __future__ import annotations

from fastapi import APIRouter, Query

from app.application.wallets.service import sync_tron_wallet_impl
from app.application.wallets.trace import trace_wallet_graph
from app.core.settings import TRON_MAX_SYNC_ITEMS
from app.domain.wallet.addresses import is_tron_address, normalize_tron_address


router = APIRouter()


@router.get("/api/tron/sync-wallet")
@router.get("/tron/sync-wallet")
async def sync_tron_wallet(
    address: str,
    days: int = Query(31, ge=0, le=3660),
    max_items: int = Query(50, ge=1, le=TRON_MAX_SYNC_ITEMS),
    all_items: bool = Query(False),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
):
    return await sync_tron_wallet_impl(
        address=address,
        days=days,
        max_items=max_items,
        all_items=all_items,
        date_from=date_from,
        date_to=date_to,
    )


@router.get("/api/tron/validate-address")
@router.get("/tron/validate-address")
async def validate_tron_address(address: str):
    normalized_address = normalize_tron_address(address)
    return {"address": normalized_address, "valid": is_tron_address(normalized_address)}


@router.get("/api/tron/trace-wallet")
@router.get("/tron/trace-wallet")
async def trace_tron_wallet(
    address: str,
    days: int = Query(31, ge=0, le=3660),
    depth: int = Query(2, ge=1, le=3),
    asset: str = Query("all"),
    include_incoming: bool = Query(True),
    max_branches: int = Query(0, ge=0, le=50),
    max_items_per_wallet: int = Query(500, ge=50, le=1000),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
):
    return await trace_wallet_graph(
        address=address,
        days=days,
        depth=depth,
        asset=asset,
        include_incoming=include_incoming,
        max_branches=max_branches,
        max_items_per_wallet=max_items_per_wallet,
        date_from=date_from,
        date_to=date_to,
    )
