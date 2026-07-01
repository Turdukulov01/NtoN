from __future__ import annotations

import os


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


CORS_ORIGINS = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "*").split(",") if origin.strip()]

TRONSCAN_BASE_URL = os.getenv("TRONSCAN_BASE_URL", "https://apilist.tronscanapi.com/api").rstrip("/")
TRONSCAN_API_KEY = os.getenv("TRONSCAN_API_KEY", "")
ETHERSCAN_BASE_URL = os.getenv("ETHERSCAN_BASE_URL", "https://api.etherscan.io/v2/api").rstrip("/")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")
ETHEREUM_CHAIN_ID = os.getenv("ETHEREUM_CHAIN_ID", "1")
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY", "")
ALCHEMY_ETH_URL = (
    os.getenv("ALCHEMY_ETH_URL")
    or (f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}" if ALCHEMY_API_KEY else "")
).rstrip("/")
BLOCKSTREAM_BASE_URL = os.getenv("BLOCKSTREAM_BASE_URL", "https://blockstream.info/api").rstrip("/")
RANEX_BASE_URL = os.getenv("RANEX_BASE_URL", "https://kyt-api.ranex.asia").rstrip("/")
RANEX_API_KEY = os.getenv("RANEX_API_KEY", "")
RANEX_TIMEOUT_SECONDS = _float_env("RANEX_TIMEOUT_SECONDS", 60.0)
SHARD_BASE_URL = os.getenv("SHARD_BASE_URL", "https://shard.ru").rstrip("/")
SHARD_PUBLIC_APP_ID = os.getenv("SHARD_PUBLIC_APP_ID", "")
SHARD_API_SECRET = os.getenv("SHARD_API_SECRET", "")
SHARD_TIMEOUT_SECONDS = _float_env("SHARD_TIMEOUT_SECONDS", 60.0)
KYT_PROVIDER_CACHE_TTL_SECONDS = _int_env("KYT_PROVIDER_CACHE_TTL_SECONDS", 86400)

TRON_PAGE_LIMIT = 50
ETHERSCAN_PAGE_LIMIT = _int_env("ETHERSCAN_PAGE_LIMIT", 1000)
BITCOIN_PAGE_LIMIT = 25
TRONSCAN_CONCURRENCY = _int_env("TRONSCAN_CONCURRENCY", 4)
TRONSCAN_MIN_INTERVAL = _float_env("TRONSCAN_MIN_INTERVAL", 0.35)
TRON_MAX_SYNC_ITEMS = _int_env("TRON_MAX_SYNC_ITEMS", 50000)
WALLET_MAX_SYNC_ITEMS = _int_env("WALLET_MAX_SYNC_ITEMS", TRON_MAX_SYNC_ITEMS)
TRON_DETAIL_ENRICH_LIMIT = _int_env("TRON_DETAIL_ENRICH_LIMIT", 500)
TRON_TRACE_MAX_SECONDS = _float_env("TRON_TRACE_MAX_SECONDS", 35)
TRON_TRACE_MAX_ADDRESSES = _int_env("TRON_TRACE_MAX_ADDRESSES", 50)
TRON_TRACE_MAX_EDGES = _int_env("TRON_TRACE_MAX_EDGES", 600)
TRON_TRACE_CANDIDATE_MULTIPLIER = _int_env("TRON_TRACE_CANDIDATE_MULTIPLIER", 3)
TRON_TRACE_ALL_TIME_MAX_ITEMS_PER_WALLET = _int_env("TRON_TRACE_ALL_TIME_MAX_ITEMS_PER_WALLET", 200)

STABLE_SYMBOLS = {"USDT", "USDC", "TUSD", "USDD", "USDJ"}
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BASE58_INDEX = {char: index for index, char in enumerate(BASE58_ALPHABET)}
TRON_MAINNET_PREFIX = 0x41
