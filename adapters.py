"""
app/adapters/ — One adapter per blockchain network.
Each adapter implements BaseAdapter and returns normalised NormalisedTx objects.
"""
from __future__ import annotations
import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import AsyncIterator, Optional
import httpx

from app.core.config import get_settings
from app.core.redis_client import get_limiter

settings = get_settings()
logger = logging.getLogger(__name__)

SATOSHI = Decimal("1e-8")
WEI = Decimal("1e-18")
SUN = Decimal("1e-6")


@dataclass
class NormalisedTx:
    """Single normalised transaction across all networks."""
    tx_hash: str
    block_number: Optional[int]
    block_timestamp: datetime
    from_address: str
    to_address: str
    amount: Decimal               # human-readable (BTC / ETH / TRX / token amount)
    raw_amount: int               # smallest unit (satoshi / wei / sun)
    asset_symbol: str
    asset_contract: Optional[str] # None for native coins
    is_native: bool
    fee_amount: Optional[Decimal]
    is_error: bool
    raw_data: dict


# ─── Base ─────────────────────────────────────────────────────────────────────

class BaseAdapter(ABC):
    network: str
    MAX_RETRIES = settings.MAX_RETRY
    BACKOFF_BASE = settings.BACKOFF_BASE

    def __init__(self):
        self.limiter = get_limiter(self.network)
        self._client: httpx.AsyncClient | None = None

    async def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def _get(self, url: str, params: dict | None = None) -> dict:
        """Rate-limited GET with exponential backoff retry."""
        for attempt in range(self.MAX_RETRIES):
            await self.limiter.acquire()
            try:
                c = await self.client()
                r = await c.get(url, params=params)
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                if attempt == self.MAX_RETRIES - 1:
                    raise
                wait = self.BACKOFF_BASE ** attempt
                logger.warning(f"[{self.network}] Retry {attempt+1} after {wait}s: {e}")
                await asyncio.sleep(wait)
        raise RuntimeError("Unreachable")

    @abstractmethod
    async def fetch_transactions(
        self,
        address: str,
        from_block: int = 0,
        from_tx: Optional[str] = None,
    ) -> AsyncIterator[NormalisedTx]:
        """Yield normalised transactions incrementally."""
        ...

    @abstractmethod
    async def get_balance(self, address: str) -> Decimal:
        ...


# ─── Bitcoin (Blockstream API) ────────────────────────────────────────────────

class BitcoinAdapter(BaseAdapter):
    network = "bitcoin"
    BASE = settings.BLOCKSTREAM_BASE_URL

    async def fetch_transactions(
        self,
        address: str,
        from_block: int = 0,
        from_tx: Optional[str] = None,
    ) -> AsyncIterator[NormalisedTx]:
        last_seen_txid = from_tx
        while True:
            url = f"{self.BASE}/address/{address}/txs"
            if last_seen_txid:
                url = f"{self.BASE}/address/{address}/txs/chain/{last_seen_txid}"
            data = await self._get(url)
            if not data:
                break

            for tx in data:
                block_ts = tx.get("status", {}).get("block_time")
                if not block_ts:
                    continue
                block_number = tx.get("status", {}).get("block_height", 0)
                if block_number and block_number <= from_block:
                    return

                # Calculate net value for this address
                in_value = sum(
                    v["value"] for v in tx.get("vout", [])
                    if any(a.get("address") == address for a in [v.get("scriptpubkey_address", {})])
                    or v.get("scriptpubkey_address") == address
                )
                out_value = sum(
                    v["value"] for vin in tx.get("vin", [])
                    if vin.get("prevout", {}).get("scriptpubkey_address") == address
                    for v in [vin.get("prevout", {})]
                )
                net = in_value - out_value
                direction_amount = abs(net)

                yield NormalisedTx(
                    tx_hash=tx["txid"],
                    block_number=block_number,
                    block_timestamp=datetime.fromtimestamp(block_ts, tz=timezone.utc),
                    from_address=address if net < 0 else "external",
                    to_address=address if net >= 0 else "external",
                    amount=Decimal(direction_amount) * SATOSHI,
                    raw_amount=direction_amount,
                    asset_symbol="BTC",
                    asset_contract=None,
                    is_native=True,
                    fee_amount=Decimal(tx.get("fee", 0)) * SATOSHI,
                    is_error=False,
                    raw_data=tx,
                )
            last_seen_txid = data[-1]["txid"]
            if len(data) < 25:   # Blockstream returns 25 per page
                break

    async def get_balance(self, address: str) -> Decimal:
        data = await self._get(f"{self.BASE}/address/{address}")
        funded = data.get("chain_stats", {}).get("funded_txo_sum", 0)
        spent = data.get("chain_stats", {}).get("spent_txo_sum", 0)
        return Decimal(funded - spent) * SATOSHI


# ─── Ethereum (Etherscan) ─────────────────────────────────────────────────────

class EthereumAdapter(BaseAdapter):
    network = "ethereum"
    BASE = "https://api.etherscan.io/api"

    async def _etherscan(self, params: dict) -> list:
        params["apikey"] = settings.ETHERSCAN_API_KEY
        data = await self._get(self.BASE, params=params)
        if data.get("status") == "0" and data.get("message") != "No transactions found":
            raise RuntimeError(f"Etherscan error: {data.get('result')}")
        result = data.get("result", [])
        return result if isinstance(result, list) else []

    async def _fetch_page(self, address: str, action: str, page: int, startblock: int) -> list:
        return await self._etherscan({
            "module": "account",
            "action": action,
            "address": address,
            "startblock": startblock,
            "endblock": 999999999,
            "page": page,
            "offset": settings.TX_PAGE_SIZE,
            "sort": "asc",
        })

    async def fetch_transactions(
        self,
        address: str,
        from_block: int = 0,
        from_tx: Optional[str] = None,
    ) -> AsyncIterator[NormalisedTx]:
        # Native ETH transactions
        async for tx in self._paginate(address, "txlist", from_block, address):
            yield tx
        # ERC-20 token transfers
        async for tx in self._paginate(address, "tokentx", from_block, address):
            yield tx

    async def _paginate(self, address: str, action: str, from_block: int, wallet_address: str):
        page = 1
        addr_lower = wallet_address.lower()
        while True:
            rows = await self._fetch_page(address, action, page, from_block)
            if not rows:
                break
            for row in rows:
                is_error = row.get("isError", "0") == "1"
                decimals = int(row.get("tokenDecimal", 18))
                raw = int(row.get("value", 0))
                amount = Decimal(raw) / Decimal(10 ** decimals)
                symbol = row.get("tokenSymbol", "ETH") if action == "tokentx" else "ETH"
                contract = row.get("contractAddress") or None

                yield NormalisedTx(
                    tx_hash=row["hash"],
                    block_number=int(row.get("blockNumber", 0)),
                    block_timestamp=datetime.fromtimestamp(int(row["timeStamp"]), tz=timezone.utc),
                    from_address=row["from"],
                    to_address=row["to"],
                    amount=amount,
                    raw_amount=raw,
                    asset_symbol=symbol,
                    asset_contract=contract,
                    is_native=(action == "txlist"),
                    fee_amount=Decimal(row.get("gasUsed", 0)) * Decimal(row.get("gasPrice", 0)) * WEI,
                    is_error=is_error,
                    raw_data=row,
                )
            if len(rows) < settings.TX_PAGE_SIZE:
                break
            page += 1

    async def get_balance(self, address: str) -> Decimal:
        data = await self._etherscan({
            "module": "account",
            "action": "balance",
            "address": address,
            "tag": "latest",
        })
        return Decimal(data[0] if isinstance(data, list) else data) * WEI


# ─── TRON (Tronscan) ──────────────────────────────────────────────────────────

class TronAdapter(BaseAdapter):
    network = "tron"
    BASE = "https://apilist.tronscan.org/api"

    async def _tron_get(self, endpoint: str, params: dict) -> dict:
        if settings.TRONSCAN_API_KEY:
            params["apikey"] = settings.TRONSCAN_API_KEY
        return await self._get(f"{self.BASE}/{endpoint}", params=params)

    async def fetch_transactions(
        self,
        address: str,
        from_block: int = 0,
        from_tx: Optional[str] = None,
    ) -> AsyncIterator[NormalisedTx]:
        # Native TRX
        async for tx in self._fetch_trx(address, from_block):
            yield tx
        # TRC-20 tokens
        async for tx in self._fetch_trc20(address, from_block):
            yield tx

    async def _fetch_trx(self, address: str, from_block: int):
        start = 0
        while True:
            data = await self._tron_get("transaction", {
                "address": address,
                "start": start,
                "limit": settings.TX_PAGE_SIZE,
                "sort": "-timestamp",
            })
            rows = data.get("data", [])
            if not rows:
                break
            for row in rows:
                block_number = row.get("block", 0)
                if block_number and block_number <= from_block:
                    return
                ts = row.get("timestamp", 0) / 1000
                contract = row.get("contractData", {})
                raw = int(contract.get("amount", 0))
                yield NormalisedTx(
                    tx_hash=row["hash"],
                    block_number=block_number,
                    block_timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                    from_address=contract.get("owner_address", ""),
                    to_address=contract.get("to_address", ""),
                    amount=Decimal(raw) * SUN,
                    raw_amount=raw,
                    asset_symbol="TRX",
                    asset_contract=None,
                    is_native=True,
                    fee_amount=Decimal(row.get("cost", {}).get("fee", 0)) * SUN,
                    is_error=row.get("contractRet") != "SUCCESS",
                    raw_data=row,
                )
            if len(rows) < settings.TX_PAGE_SIZE:
                break
            start += settings.TX_PAGE_SIZE

    async def _fetch_trc20(self, address: str, from_block: int):
        start = 0
        while True:
            data = await self._tron_get("token_trc20/transfers", {
                "relatedAddress": address,
                "start": start,
                "limit": settings.TX_PAGE_SIZE,
            })
            rows = data.get("token_transfers", [])
            if not rows:
                break
            for row in rows:
                raw = int(row.get("quant", 0))
                decimals = int(row.get("tokenInfo", {}).get("tokenDecimal", 6))
                amount = Decimal(raw) / Decimal(10 ** decimals)
                yield NormalisedTx(
                    tx_hash=row["transaction_id"],
                    block_number=row.get("block", 0),
                    block_timestamp=datetime.fromtimestamp(
                        row.get("block_ts", 0) / 1000, tz=timezone.utc
                    ),
                    from_address=row.get("from_address", ""),
                    to_address=row.get("to_address", ""),
                    amount=amount,
                    raw_amount=raw,
                    asset_symbol=row.get("tokenInfo", {}).get("tokenAbbr", "TRC20"),
                    asset_contract=row.get("contract_address"),
                    is_native=False,
                    fee_amount=None,
                    is_error=False,
                    raw_data=row,
                )
            if len(rows) < settings.TX_PAGE_SIZE:
                break
            start += settings.TX_PAGE_SIZE

    async def get_balance(self, address: str) -> Decimal:
        data = await self._tron_get("account", {"address": address})
        raw = data.get("bandwidth", {}).get("freeNetRemaining", 0)
        balance = data.get("balance", 0)
        return Decimal(balance) * SUN


# ─── Factory ──────────────────────────────────────────────────────────────────

def get_adapter(network: str) -> BaseAdapter:
    adapters = {
        "bitcoin": BitcoinAdapter,
        "ethereum": EthereumAdapter,
        "tron": TronAdapter,
    }
    cls = adapters.get(network)
    if not cls:
        raise ValueError(f"Unknown network: {network}")
    return cls()
