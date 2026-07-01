from __future__ import annotations

import asyncio
import time
from collections import defaultdict

from redis.asyncio import Redis

from app.core.config import get_settings


class AsyncRateLimiter:
    def __init__(self, min_interval: float = 0.25) -> None:
        self.min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait_for = self._last_request + self.min_interval - now
            if wait_for > 0:
                await asyncio.sleep(wait_for)
                now = time.monotonic()
            self._last_request = now


_limiters: dict[str, AsyncRateLimiter] = defaultdict(lambda: AsyncRateLimiter())
_redis: Redis | None = None


def get_limiter(network: str) -> AsyncRateLimiter:
    return _limiters[network]


async def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
    return _redis
