from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.health import router as health_router
from app.api.routes.risk import build_risk_router
from app.api.routes.sanctions import router as sanctions_router
from app.api.routes.tron import router as tron_router
from app.api.routes.wallets import router as wallets_router
from app.application.wallets.service import collect_wallet_report
from app.application.wallets.trace import trace_wallet_graph
from app.core.settings import CORS_ORIGINS


def create_app() -> FastAPI:
    app = FastAPI(
        title="ChainLens Docker API",
        version="1.0.0",
        description="Operational API shell for the available project snapshot.",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(tron_router)
    app.include_router(wallets_router)
    app.include_router(sanctions_router, tags=["Sanctions"])
    app.include_router(
        build_risk_router(
            collect_wallet_report=collect_wallet_report,
            trace_wallet_graph=trace_wallet_graph,
        ),
        tags=["Risk"],
    )

    return app


app = create_app()
