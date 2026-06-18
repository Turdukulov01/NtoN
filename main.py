"""
Blockchain Wallet Aggregator — FastAPI entry point
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.api import wallets, transactions, analytics, export, sync, auth
from app.core.database import engine, Base
from app.core.scheduler import start_scheduler, stop_scheduler
from app.core.redis_client import get_redis
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Blockchain Aggregator...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await start_scheduler()
    yield
    await stop_scheduler()
    logger.info("Shutdown complete.")


app = FastAPI(
    title="Blockchain Wallet Aggregator",
    version="1.0.0",
    description="Aggregates BTC/ETH/TRON wallet data with analytics",
    lifespan=lifespan,
)

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "https://yourdomain.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api/v1/auth", tags=["Auth"])
app.include_router(wallets.router, prefix="/api/v1/wallets", tags=["Wallets"])
app.include_router(transactions.router, prefix="/api/v1/transactions", tags=["Transactions"])
app.include_router(analytics.router, prefix="/api/v1/analytics", tags=["Analytics"])
app.include_router(export.router, prefix="/api/v1/export", tags=["Export"])
app.include_router(sync.router, prefix="/api/v1/sync", tags=["Sync"])


@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}
