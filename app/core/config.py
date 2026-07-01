from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://postgres:changeme@localhost:5432/blockchain_agg"
    REDIS_URL: str = "redis://localhost:6379/0"

    MAX_RETRY: int = 3
    BACKOFF_BASE: float = 2.0
    TX_PAGE_SIZE: int = 50
    SYNC_BATCH_SIZE: int = 5

    BLOCKSTREAM_BASE_URL: str = "https://blockstream.info/api"
    ETHERSCAN_API_KEY: str = ""
    TRONSCAN_API_KEY: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
