from __future__ import annotations

import asyncio
from typing import Any

from fastapi import HTTPException
import httpx

from app.core.formatting import first_text
from app.core.settings import (
    ALCHEMY_ETH_URL,
    BLOCKSTREAM_BASE_URL,
    ETHEREUM_CHAIN_ID,
    ETHERSCAN_API_KEY,
    ETHERSCAN_BASE_URL,
    ETHERSCAN_PAGE_LIMIT,
    TRON_DETAIL_ENRICH_LIMIT,
    TRON_PAGE_LIMIT,
    TRONSCAN_API_KEY,
    TRONSCAN_BASE_URL,
    TRONSCAN_CONCURRENCY,
    TRONSCAN_MIN_INTERVAL,
)

TRONSCAN_RATE_LOCK = asyncio.Lock()
TRONSCAN_LAST_REQUEST = 0.0

async def throttle_tronscan() -> None:
    global TRONSCAN_LAST_REQUEST
    if TRONSCAN_MIN_INTERVAL <= 0:
        return
    async with TRONSCAN_RATE_LOCK:
        loop = asyncio.get_running_loop()
        now = loop.time()
        wait_for = TRONSCAN_LAST_REQUEST + TRONSCAN_MIN_INTERVAL - now
        if wait_for > 0:
            await asyncio.sleep(wait_for)
            now = loop.time()
        TRONSCAN_LAST_REQUEST = now


async def tronscan_get(client: httpx.AsyncClient, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if TRONSCAN_API_KEY:
        headers["TRON-PRO-API-KEY"] = TRONSCAN_API_KEY

    filtered_params = {key: value for key, value in params.items() if value not in (None, "")}
    response: httpx.Response | None = None
    for attempt in range(3):
        try:
            await throttle_tronscan()
            response = await client.get(
                f"{TRONSCAN_BASE_URL}/{endpoint.lstrip('/')}",
                params=filtered_params,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Tronscan request failed: {exc}") from exc
        if response.status_code != 429 or attempt == 2:
            break
        await asyncio.sleep(1.2 * (attempt + 1))

    if response is None:
        raise HTTPException(status_code=502, detail="Tronscan request failed")

    if response.status_code >= 400:
        details = response.text[:200].replace("\n", " ")
        if response.status_code == 401 and not TRONSCAN_API_KEY:
            details = "Tronscan requires TRONSCAN_API_KEY on this server"
        raise HTTPException(status_code=502, detail=f"Tronscan HTTP {response.status_code}: {details}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"Tronscan returned non-JSON response: {response.text[:200]}") from exc

    if isinstance(payload, dict) and payload.get("code") not in (None, 200) and payload.get("message"):
        raise HTTPException(status_code=502, detail=f"Tronscan error: {payload.get('message')}")
    return payload if isinstance(payload, dict) else {}


async def etherscan_get(client: httpx.AsyncClient, params: dict[str, Any]) -> dict[str, Any]:
    if not ETHERSCAN_API_KEY:
        raise HTTPException(
            status_code=400,
            detail="Для Ethereum history нужен ETHERSCAN_API_KEY на сервере",
        )

    filtered_params = {
        "chainid": ETHEREUM_CHAIN_ID,
        **{key: value for key, value in params.items() if value not in (None, "")},
        "apikey": ETHERSCAN_API_KEY,
    }
    try:
        response = await client.get(ETHERSCAN_BASE_URL, params=filtered_params)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Etherscan request failed: {exc}") from exc

    if response.status_code >= 400:
        details = response.text[:200].replace("\n", " ")
        raise HTTPException(status_code=502, detail=f"Etherscan HTTP {response.status_code}: {details}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"Etherscan returned non-JSON response: {response.text[:200]}") from exc

    if not isinstance(payload, dict):
        return {}

    status = str(payload.get("status") or "")
    message = str(payload.get("message") or "")
    result = payload.get("result")
    empty_ok = (
        status == "0"
        and isinstance(result, str)
        and ("No transactions found" in result or "No records found" in result or "No transactions found" in message)
    )
    if status == "0" and not empty_ok and message.upper() in {"NOTOK", "ERROR"}:
        detail = str(result or message)[:200]
        raise HTTPException(status_code=502, detail=f"Etherscan error: {detail}")
    return payload


async def fetch_etherscan_account_rows(
    client: httpx.AsyncClient,
    action: str,
    address: str,
    max_items: int,
) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    page = 1
    truncated = False
    while len(rows) < max_items:
        offset = max(1, min(ETHERSCAN_PAGE_LIMIT, max_items - len(rows)))
        payload = await etherscan_get(
            client,
            {
                "module": "account",
                "action": action,
                "address": address,
                "startblock": 0,
                "endblock": 99999999,
                "page": page,
                "offset": offset,
                "sort": "desc",
            },
        )
        result = payload.get("result")
        page_rows = result if isinstance(result, list) else []
        if not page_rows:
            break
        rows.extend(page_rows)
        if len(page_rows) < offset:
            break
        page += 1
        if len(rows) >= max_items:
            truncated = True
            break
    return rows[:max_items], truncated


async def alchemy_rpc(client: httpx.AsyncClient, method: str, params: list[Any]) -> Any:
    if not ALCHEMY_ETH_URL:
        raise HTTPException(status_code=400, detail="ALCHEMY_API_KEY или ALCHEMY_ETH_URL не настроен")
    try:
        response = await client.post(
            ALCHEMY_ETH_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Alchemy request failed: {exc}") from exc
    if response.status_code >= 400:
        details = response.text[:200].replace("\n", " ")
        raise HTTPException(status_code=502, detail=f"Alchemy HTTP {response.status_code}: {details}")
    payload = response.json()
    if payload.get("error"):
        raise HTTPException(status_code=502, detail=f"Alchemy error: {payload['error']}")
    return payload.get("result")


async def blockstream_get(client: httpx.AsyncClient, path: str) -> Any:
    try:
        response = await client.get(f"{BLOCKSTREAM_BASE_URL}/{path.lstrip('/')}")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Blockstream request failed: {exc}") from exc
    if response.status_code >= 400:
        details = response.text[:200].replace("\n", " ")
        raise HTTPException(status_code=502, detail=f"Blockstream HTTP {response.status_code}: {details}")
    try:
        return response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"Blockstream returned non-JSON response: {response.text[:200]}") from exc


async def fetch_tronscan_pages(
    client: httpx.AsyncClient,
    endpoint: str,
    list_key: str,
    params: dict[str, Any],
    max_items: int,
) -> tuple[list[dict[str, Any]], bool]:
    def extract_page(payload: dict[str, Any]) -> list[dict[str, Any]]:
        page = payload.get(list_key)
        if page is None:
            page = payload.get("data", [])
        return page if isinstance(page, list) else []

    first_limit = min(TRON_PAGE_LIMIT, max_items)
    first_payload = await tronscan_get(client, endpoint, {**params, "start": 0, "limit": first_limit})
    rows = extract_page(first_payload)
    if not rows:
        return [], False

    range_total = int(first_payload.get("rangeTotal") or first_payload.get("total") or len(rows))
    total_to_fetch = min(range_total, max_items)
    if len(rows) >= total_to_fetch or len(rows) < first_limit:
        return rows[:max_items], range_total > len(rows)

    semaphore = asyncio.Semaphore(TRONSCAN_CONCURRENCY)

    async def fetch_page(start: int) -> tuple[int, list[dict[str, Any]]]:
        async with semaphore:
            limit = min(TRON_PAGE_LIMIT, total_to_fetch - start)
            payload = await tronscan_get(client, endpoint, {**params, "start": start, "limit": limit})
            return start, extract_page(payload)

    starts = list(range(len(rows), total_to_fetch, TRON_PAGE_LIMIT))
    pages = await asyncio.gather(*(fetch_page(start) for start in starts))
    for _, page in sorted(pages, key=lambda item: item[0]):
        rows.extend(page)

    return rows[:max_items], range_total > len(rows)


async def enrich_missing_trx_recipients(client: httpx.AsyncClient, rows: list[dict[str, Any]]) -> None:
    missing_rows = [
        row
        for row in rows
        if row.get("hash")
        and not first_text(row, "to", "to_address", "transferToAddress", "toAddress")
    ][:TRON_DETAIL_ENRICH_LIMIT]
    if not missing_rows:
        return

    semaphore = asyncio.Semaphore(max(1, min(TRONSCAN_CONCURRENCY, 3)))

    async def fetch_details(row: dict[str, Any]) -> None:
        async with semaphore:
            try:
                row["_details"] = await tronscan_get(client, "transaction-info", {"hash": row.get("hash")})
            except HTTPException:
                row["_details"] = {}

    await asyncio.gather(*(fetch_details(row) for row in missing_rows))


def merge_transaction_details(rows: list[dict[str, Any]], detail_rows: list[dict[str, Any]]) -> None:
    details_by_hash = {
        str(row.get("hash") or ""): row
        for row in detail_rows
        if row.get("hash")
    }
    if not details_by_hash:
        return

    for row in rows:
        row_hash = str(row.get("hash") or "")
        if row_hash in details_by_hash:
            row["_details"] = details_by_hash[row_hash]
