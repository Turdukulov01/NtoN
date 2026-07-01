from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.application.wallets.service import collect_wallet_report
from app.core.settings import WALLET_MAX_SYNC_ITEMS
from app.domain.wallet.addresses import normalize_detected_wallet_address, normalize_network_key, normalize_wallet_address


router = APIRouter()


@router.get("/api/wallet/validate-address")
@router.get("/wallet/validate-address")
async def validate_wallet_address(
    address: str = Query(...),
    network: str = Query("all"),
):
    network_key = normalize_network_key(network)
    if network_key in {"all", "*"}:
        detected = normalize_detected_wallet_address(address)
        if not detected:
            return {
                "valid": False,
                "network": None,
                "address": address,
                "detail": "Некорректный адрес. Поддерживаются TRON, Ethereum и Bitcoin mainnet.",
            }
        detected_network, normalized_address = detected
        return {"valid": True, "network": detected_network, "address": normalized_address}

    try:
        normalized_address = normalize_wallet_address(network_key, address)
    except HTTPException as exc:
        return {
            "valid": False,
            "network": network_key,
            "address": address,
            "detail": getattr(exc, "detail", "Некорректный адрес кошелька"),
        }

    return {"valid": True, "network": network_key, "address": normalized_address}


@router.get("/api/wallet/collect")
@router.get("/wallet/collect")
async def collect_wallet(
    network: str = Query(...),
    address: str = Query(...),
    days: int = Query(0, ge=0, le=3660),
    max_items: int = Query(50, ge=1, le=WALLET_MAX_SYNC_ITEMS),
    all_items: bool = Query(False),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
):
    return await collect_wallet_report(
        network=network,
        address=address,
        days=days,
        max_items=max_items,
        all_items=all_items,
        date_from=date_from,
        date_to=date_to,
    )
