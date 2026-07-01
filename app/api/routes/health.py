from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse


router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    return (
        "# HELP chainlens_up Whether the Docker API shell is running.\n"
        "# TYPE chainlens_up gauge\n"
        "chainlens_up 1\n"
    )
