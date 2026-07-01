from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
import httpx

from app.core.formatting import amount_from_base_units, build_price_maps, decimal_or_zero, decimal_to_str, first_text
from app.core.periods import build_sync_period, filter_transactions_by_period, parse_sync_date_boundary
from app.core.settings import (
    ALCHEMY_ETH_URL,
    BITCOIN_PAGE_LIMIT,
    TRON_DETAIL_ENRICH_LIMIT,
    TRON_MAX_SYNC_ITEMS,
    TRONSCAN_API_KEY,
    WALLET_MAX_SYNC_ITEMS,
)
from app.domain.wallet.addresses import is_tron_address, normalize_bitcoin_address, normalize_ethereum_address, normalize_tron_address, normalize_wallet_address
from app.domain.wallet.analytics import build_balances_from_summary, build_common_wallet_report, build_summary
from app.infrastructure.blockchain.clients import blockstream_get, enrich_missing_trx_recipients, etherscan_get, fetch_etherscan_account_rows, fetch_tronscan_pages, merge_transaction_details, tronscan_get, alchemy_rpc
from app.infrastructure.blockchain.normalizers import build_tron_balances, normalize_bitcoin_transaction, normalize_ethereum_native_transfer, normalize_ethereum_token_transfer, normalize_trc20_transfer, normalize_trx_transfer

async def collect_ethereum_wallet_impl(
    address: str,
    days: int = 0,
    max_items: int = 50,
    all_items: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    address = normalize_ethereum_address(address)
    start, end, period = build_sync_period(days, date_from, date_to)
    requested_limit = min(max_items, WALLET_MAX_SYNC_ITEMS)

    timeout = httpx.Timeout(45.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        balances: list[dict[str, Any]] = []
        try:
            balance_payload = await etherscan_get(
                client,
                {
                    "module": "account",
                    "action": "balance",
                    "address": address,
                    "tag": "latest",
                },
            )
            eth_balance = amount_from_base_units(balance_payload.get("result"), 18)
            balances.append(
                {
                    "asset": "ETH",
                    "amount": decimal_to_str(eth_balance, 18),
                    "usd_value": "",
                    "contract_address": "",
                    "is_native": True,
                    "estimated": False,
                }
            )
        except HTTPException:
            if ALCHEMY_ETH_URL:
                raw_balance = await alchemy_rpc(client, "eth_getBalance", [address, "latest"])
                eth_balance = Decimal(int(str(raw_balance or "0x0"), 16)) / (Decimal(10) ** 18)
                balances.append(
                    {
                        "asset": "ETH",
                        "amount": decimal_to_str(eth_balance, 18),
                        "usd_value": "",
                        "contract_address": "",
                        "is_native": True,
                        "estimated": False,
                    }
                )
            else:
                raise

        native_rows, native_truncated = await fetch_etherscan_account_rows(client, "txlist", address, requested_limit)
        token_rows, token_truncated = await fetch_etherscan_account_rows(client, "tokentx", address, requested_limit)

    fetched_transactions = [
        normalize_ethereum_native_transfer(row, address, index)
        for index, row in enumerate(native_rows)
        if decimal_or_zero(row.get("value")) > 0
    ] + [
        normalize_ethereum_token_transfer(row, address, index)
        for index, row in enumerate(token_rows)
    ]
    fetched_transactions.sort(key=lambda tx: tx.get("timestamp") or "", reverse=True)
    filtered_transactions = filter_transactions_by_period(fetched_transactions, start, end)
    combined_truncated = len(filtered_transactions) > requested_limit
    transactions = filtered_transactions[:requested_limit]

    for row in build_balances_from_summary(build_summary(transactions)):
        if row.get("asset") == "ETH":
            continue
        row["estimated"] = True
        row["note"] = "estimated token net flow from loaded transactions"
        balances.append(row)

    return build_common_wallet_report(
        network="ethereum",
        address=address,
        source="etherscan",
        period=period,
        transactions=transactions,
        balances=balances,
        truncated=native_truncated or token_truncated or combined_truncated,
        counts_extra={
            "native": len(native_rows),
            "erc20": len(token_rows),
            "fetched_before_slice": len(fetched_transactions),
            "max_items": requested_limit,
            "all_items_requested": all_items,
        },
    )


async def collect_bitcoin_wallet_impl(
    address: str,
    days: int = 0,
    max_items: int = 50,
    all_items: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    address = normalize_bitcoin_address(address)
    start, end, period = build_sync_period(days, date_from, date_to)
    requested_limit = min(max_items, WALLET_MAX_SYNC_ITEMS)

    timeout = httpx.Timeout(45.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        address_payload = await blockstream_get(client, f"address/{address}")
        chain_stats = address_payload.get("chain_stats") if isinstance(address_payload, dict) else {}
        mempool_stats = address_payload.get("mempool_stats") if isinstance(address_payload, dict) else {}
        funded_sats = decimal_or_zero(chain_stats.get("funded_txo_sum")) + decimal_or_zero(mempool_stats.get("funded_txo_sum"))
        spent_sats = decimal_or_zero(chain_stats.get("spent_txo_sum")) + decimal_or_zero(mempool_stats.get("spent_txo_sum"))
        balance_btc = (funded_sats - spent_sats) / Decimal("100000000")
        balances = [
            {
                "asset": "BTC",
                "amount": decimal_to_str(balance_btc, 8),
                "usd_value": "",
                "contract_address": "",
                "is_native": True,
                "estimated": False,
            }
        ]

        mempool_page = await blockstream_get(client, f"address/{address}/txs/mempool")
        mempool_rows = mempool_page if isinstance(mempool_page, list) else []
        raw_rows: list[dict[str, Any]] = mempool_rows[:requested_limit]
        target_raw_count = min(WALLET_MAX_SYNC_ITEMS, requested_limit + len(raw_rows))
        last_seen_txid = ""
        truncated = False
        while len(raw_rows) < target_raw_count:
            path = f"address/{address}/txs/chain"
            if last_seen_txid:
                path = f"{path}/{last_seen_txid}"
            page = await blockstream_get(client, path)
            page_rows = page if isinstance(page, list) else []
            if not page_rows:
                break
            raw_rows.extend(page_rows)
            last_seen_txid = str(page_rows[-1].get("txid") or "")
            if len(page_rows) < BITCOIN_PAGE_LIMIT or not last_seen_txid:
                break
            if len(raw_rows) >= target_raw_count:
                truncated = True
                break

    fetched_transactions = [
        tx
        for index, row in enumerate(raw_rows)
        if (tx := normalize_bitcoin_transaction(row, address, index))
    ]
    fetched_transactions.sort(key=lambda tx: tx.get("timestamp") or "", reverse=True)
    filtered_transactions = filter_transactions_by_period(fetched_transactions, start, end)
    combined_truncated = len(filtered_transactions) > requested_limit
    transactions = filtered_transactions[:requested_limit]

    return build_common_wallet_report(
        network="bitcoin",
        address=address,
        source="blockstream",
        period=period,
        transactions=transactions,
        balances=balances,
        truncated=truncated or combined_truncated,
        counts_extra={
            "fetched_before_slice": len(fetched_transactions),
            "max_items": requested_limit,
            "chain_tx_count": chain_stats.get("tx_count") or 0,
            "mempool_tx_count": mempool_stats.get("tx_count") or 0,
            "mempool_fetched": len(mempool_rows),
            "all_items_requested": all_items,
        },
    )


async def sync_tron_wallet_impl(
    address: str,
    days: int = 31,
    max_items: int = 50,
    all_items: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    address = normalize_tron_address(address)
    if not is_tron_address(address):
        raise HTTPException(status_code=400, detail="Некорректный TRON-адрес")

    end = datetime.now(timezone.utc)
    range_start = parse_sync_date_boundary(date_from, end_of_day=False)
    range_end = parse_sync_date_boundary(date_to, end_of_day=True)
    if range_start or range_end:
        start = range_start
        end = range_end or end
        if start and start > end:
            raise HTTPException(status_code=400, detail="Дата начала не может быть позже даты окончания")
    else:
        start = end - timedelta(days=days) if days > 0 else None

    time_params: dict[str, Any] = {}
    if start:
        time_params["start_timestamp"] = int(start.timestamp() * 1000)
    if start or range_end:
        time_params["end_timestamp"] = int(end.timestamp() * 1000)

    requested_limit = min(max_items, TRON_MAX_SYNC_ITEMS)

    timeout = httpx.Timeout(45.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        wallet_payload = await tronscan_get(client, "account/wallet", {"address": address})
        prices_by_token, prices_by_symbol = build_price_maps(wallet_payload)

        trx_rows, trx_truncated = await fetch_tronscan_pages(
            client,
            "transfer/trx",
            "data",
            {
                "address": address,
                "direction": 0,
                "reverse": "true",
                "fee": "true",
                "db_version": 1,
                **time_params,
            },
            requested_limit,
        )
        if any(not first_text(row, "to", "to_address", "transferToAddress", "toAddress") for row in trx_rows):
            detail_rows, _ = await fetch_tronscan_pages(
                client,
                "transaction",
                "data",
                {
                    "address": address,
                    "sort": "-timestamp",
                    "count": "true",
                    **time_params,
                },
                min(requested_limit, TRON_DETAIL_ENRICH_LIMIT),
            )
            merge_transaction_details(trx_rows, detail_rows)
            if TRONSCAN_API_KEY:
                await enrich_missing_trx_recipients(client, trx_rows)
        trc20_rows, trc20_truncated = await fetch_tronscan_pages(
            client,
            "token_trc20/transfers",
            "token_transfers",
            {
                "relatedAddress": address,
                **time_params,
            },
            requested_limit,
        )

    fetched_transactions = [
        normalize_trx_transfer(row, address, prices_by_symbol, index)
        for index, row in enumerate(trx_rows)
    ] + [
        normalize_trc20_transfer(row, address, prices_by_token, prices_by_symbol, index)
        for index, row in enumerate(trc20_rows)
    ]
    fetched_transactions.sort(key=lambda tx: tx.get("timestamp") or "", reverse=True)
    combined_truncated = len(fetched_transactions) > requested_limit
    transactions = fetched_transactions[:requested_limit]

    return {
        "address": address,
        "period": {
            "days": days,
            "all_time": days == 0 and not (range_start or range_end),
            "start": start.isoformat().replace("+00:00", "Z") if start else None,
            "end": end.isoformat().replace("+00:00", "Z"),
            "date_from": date_from,
            "date_to": date_to,
        },
        "counts": {
            "trx": len(trx_rows),
            "trc20": len(trc20_rows),
            "total": len(transactions),
            "fetched_before_slice": len(fetched_transactions),
            "max_items": requested_limit,
        },
        "truncated": trx_truncated or trc20_truncated or combined_truncated,
        "all_items_requested": all_items,
        "summary": build_summary(transactions),
        "balances": build_tron_balances(wallet_payload),
        "transactions": transactions,
        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

async def collect_wallet_report(
    *,
    network: str,
    address: str,
    days: int = 0,
    max_items: int = 50,
    all_items: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    network_key = network.strip().lower()
    network_key = {"trx": "tron", "eth": "ethereum", "btc": "bitcoin"}.get(network_key, network_key)
    address = normalize_wallet_address(network_key, address)

    if network_key == "tron":
        payload = await sync_tron_wallet_impl(
            address=address,
            days=days,
            max_items=min(max_items, TRON_MAX_SYNC_ITEMS),
            all_items=all_items,
            date_from=date_from,
            date_to=date_to,
        )
        return build_common_wallet_report(
            network="tron",
            address=payload["address"],
            source="tronscan",
            period=payload["period"],
            transactions=payload.get("transactions", []),
            balances=payload.get("balances", []),
            truncated=bool(payload.get("truncated")),
            counts_extra={
                **(payload.get("counts") if isinstance(payload.get("counts"), dict) else {}),
                "all_items_requested": all_items,
            },
            fetched_at=payload.get("fetched_at"),
        )

    if network_key == "ethereum":
        return await collect_ethereum_wallet_impl(
            address=address,
            days=days,
            max_items=max_items,
            all_items=all_items,
            date_from=date_from,
            date_to=date_to,
        )

    if network_key == "bitcoin":
        return await collect_bitcoin_wallet_impl(
            address=address,
            days=days,
            max_items=max_items,
            all_items=all_items,
            date_from=date_from,
            date_to=date_to,
        )

    raise HTTPException(status_code=400, detail="Поддерживаются только сети: tron, ethereum, bitcoin")
