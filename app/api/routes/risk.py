from __future__ import annotations

from uuid import UUID
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.application.risk.service import (
    address_risk_tag_event_to_dict,
    address_risk_tag_to_dict,
    assess_wallet_risk_pipeline,
    delete_address_risk_tag,
    delete_kyt_provider_report,
    get_kyt_provider_report,
    json_safe,
    list_address_risk_tags,
    list_address_risk_tag_events,
    list_kyt_provider_reports,
    kyt_provider_report_to_dict,
    upsert_address_risk_tag,
)
from app.core.database import AsyncSessionLocal
from app.domain.risk.engine import load_risk_model
from app.domain.wallet.addresses import normalize_detected_wallet_address, normalize_wallet_address as normalize_chain_address
from app.models import RiskAssessment, RiskAssessmentEvidence


CollectWalletReport = Callable[..., Awaitable[dict[str, Any]]]
TraceWalletGraph = Callable[..., Awaitable[dict[str, Any]]]


class RiskAssessWalletRequest(BaseModel):
    address: str = Field(..., min_length=8)
    chain: str = Field("tron")
    days: int = Field(0, ge=0, le=3660)
    max_items: int = Field(500, ge=1, le=50000)
    all_items: bool = True
    skip_wallet_collection: bool = False
    date_from: str | None = None
    date_to: str | None = None

    include_trace: bool = True
    trace_depth: int = Field(1, ge=1, le=3)
    trace_max_branches: int = Field(25, ge=0, le=50)
    trace_max_items_per_wallet: int = Field(200, ge=50, le=1000)

    participant_profile: dict[str, Any] = Field(default_factory=dict)
    control_profile: dict[str, Any] = Field(default_factory=dict)
    transaction_profile: dict[str, Any] = Field(default_factory=dict)
    wallet_profile: dict[str, Any] = Field(default_factory=dict)
    risk_tags: list[Any] = Field(default_factory=list)
    tagged_addresses: dict[str, Any] | list[dict[str, Any]] = Field(default_factory=dict)
    screening_subjects: list[dict[str, Any]] = Field(default_factory=list)
    external_kyt: dict[str, Any] = Field(default_factory=dict)
    kyt_provider: str | None = Field(None, description="Optional external KYT provider, e.g. ranex")
    score_mode: str = Field("model", description="model or provider")
    external_kyt_required: bool = False
    force_kyt_refresh: bool = False
    sanctions_screening_limit: int = Field(25, ge=1, le=100)
    persist_assessment: bool = True


class AddressRiskTagRequest(BaseModel):
    address: str = Field(..., min_length=8)
    chain: str = Field("tron")
    tag: str = Field(..., min_length=1)
    source: str = Field("manual")
    label: str | None = None
    confidence: float | None = Field(None, ge=0, le=100)
    note: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True
    actor: str | None = Field("analyst")


def _request_dict(request: BaseModel) -> dict[str, Any]:
    if hasattr(request, "model_dump"):
        return request.model_dump()
    return request.dict()


def _normalize_chain(chain: str) -> str:
    key = chain.strip().lower()
    return {"trx": "tron", "eth": "ethereum", "btc": "bitcoin"}.get(key, key)


def _validate_risk_address(chain: str, address: str) -> str:
    if chain in {"all", "*"}:
        detected = normalize_detected_wallet_address(address)
        if not detected:
            raise HTTPException(
                status_code=400,
                detail="Некорректный адрес. Поддерживаются TRON, Ethereum и Bitcoin mainnet.",
            )
        return detected[1]
    return normalize_chain_address(chain, address)


def build_risk_router(
    *,
    collect_wallet_report: CollectWalletReport,
    trace_wallet_graph: TraceWalletGraph,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/risk/model")
    @router.get("/risk/model")
    async def risk_model_metadata():
        model = load_risk_model()
        return {
            "model": model.get("model"),
            "weights": model.get("weights"),
            "zones": model.get("zones"),
            "risk_tags": model.get("risk_tags"),
            "override_rules": model.get("override_rules"),
            "data_sources": model.get("data_sources"),
        }

    @router.get("/api/risk/jurisdictions")
    @router.get("/risk/jurisdictions")
    async def risk_jurisdictions():
        model = load_risk_model()
        return ((model.get("data_sources") or {}).get("fatf_jurisdictions") or {})

    @router.get("/api/risk/address-tags")
    @router.get("/risk/address-tags")
    async def risk_address_tags(
        address: str | None = None,
        chain: str | None = None,
        tag: str | None = None,
        active_only: bool = False,
        limit: int = 100,
    ):
        network = _normalize_chain(chain) if chain else None
        async with AsyncSessionLocal() as session:
            rows = await list_address_risk_tags(
                session,
                network=network,
                address=address,
                tag=tag,
                active_only=active_only,
                limit=max(1, min(limit, 1000)),
            )
        return [address_risk_tag_to_dict(row) for row in rows]

    @router.get("/api/risk/address-tags/{tag_id}/events")
    @router.get("/risk/address-tags/{tag_id}/events")
    async def risk_address_tag_events(tag_id: UUID, limit: int = 100):
        async with AsyncSessionLocal() as session:
            rows = await list_address_risk_tag_events(
                session,
                address_risk_tag_id=tag_id,
                limit=max(1, min(limit, 1000)),
            )
        return [address_risk_tag_event_to_dict(row) for row in rows]

    @router.post("/api/risk/address-tags")
    @router.post("/risk/address-tags")
    async def risk_address_tag_upsert(request: AddressRiskTagRequest):
        network = _normalize_chain(request.chain)
        if network not in {"tron", "ethereum", "bitcoin", "all", "*"}:
            raise HTTPException(status_code=400, detail="Поддерживаются только сети: tron, ethereum, bitcoin")
        validated_address = _validate_risk_address(network, request.address)
        async with AsyncSessionLocal() as session:
            row = await upsert_address_risk_tag(
                session,
                network=network,
                address=validated_address,
                tag=request.tag,
                source=request.source,
                label=request.label,
                confidence=request.confidence,
                note=request.note,
                metadata_json=request.metadata_json,
                is_active=request.is_active,
                actor=request.actor,
            )
        return address_risk_tag_to_dict(row)

    @router.delete("/api/risk/address-tags/{tag_id}")
    @router.delete("/risk/address-tags/{tag_id}")
    async def risk_address_tag_delete(tag_id: UUID):
        async with AsyncSessionLocal() as session:
            deleted = await delete_address_risk_tag(session, tag_id=tag_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Риск-метка не найдена")
        return {"deleted": True, "risk_tag": deleted}

    @router.get("/api/risk/kyt-reports")
    @router.get("/risk/kyt-reports")
    async def risk_kyt_reports(
        address: str | None = None,
        chain: str | None = None,
        provider: str | None = None,
        limit: int = 50,
    ):
        network = _normalize_chain(chain) if chain else None
        async with AsyncSessionLocal() as session:
            rows = await list_kyt_provider_reports(
                session,
                provider=provider.strip().lower() if provider else None,
                network=network,
                address=address,
                limit=max(1, min(limit, 200)),
            )
        return [kyt_provider_report_to_dict(row) for row in rows]

    @router.get("/api/risk/kyt-reports/{report_id}")
    @router.get("/risk/kyt-reports/{report_id}")
    async def risk_kyt_report_get(
        report_id: UUID,
        include_raw: bool = False,
        include_normalized: bool = True,
    ):
        async with AsyncSessionLocal() as session:
            report = await get_kyt_provider_report(session, report_id=report_id)
            if report is None:
                raise HTTPException(status_code=404, detail="KYT provider report not found")
            return kyt_provider_report_to_dict(
                report,
                include_raw=include_raw,
                include_normalized=include_normalized,
            )

    @router.delete("/api/risk/kyt-reports/{report_id}")
    @router.delete("/risk/kyt-reports/{report_id}")
    async def risk_kyt_report_delete(report_id: UUID):
        async with AsyncSessionLocal() as session:
            deleted = await delete_kyt_provider_report(session, report_id=report_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="KYT-проверка не найдена")
        return {"deleted": True, "id": str(report_id)}

    @router.get("/api/risk/assessments/{assessment_id}")
    @router.get("/risk/assessments/{assessment_id}")
    async def risk_assessment_get(assessment_id: UUID):
        async with AsyncSessionLocal() as session:
            assessment = await session.get(RiskAssessment, assessment_id)
            if assessment is None:
                raise HTTPException(status_code=404, detail="Risk assessment not found")
            evidence = (
                await session.execute(
                    select(RiskAssessmentEvidence)
                    .where(RiskAssessmentEvidence.assessment_id == assessment.id)
                    .order_by(RiskAssessmentEvidence.id)
                )
            ).scalars().all()
        return {
            "id": str(assessment.id),
            "network": assessment.network,
            "address": assessment.address,
            "model_version": assessment.model_version,
            "raw_score": float(assessment.raw_score),
            "final_score": float(assessment.final_score),
            "risk_zone": assessment.risk_zone,
            "category": assessment.category,
            "override_hit": assessment.override_hit,
            "override_reasons": assessment.override_reasons or [],
            "result": assessment.result_payload,
            "sanctions_screening": assessment.sanctions_screening,
            "data_quality": assessment.data_quality or {},
            "created_at": assessment.created_at.isoformat() if assessment.created_at else None,
            "evidence": [
                {
                    "id": row.id,
                    "evidence_type": row.evidence_type,
                    "source": row.source,
                    "subject_type": row.subject_type,
                    "subject_value": row.subject_value,
                    "payload": row.payload,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in evidence
            ],
        }

    @router.post("/api/risk/assess-wallet")
    @router.post("/risk/assess-wallet")
    async def assess_wallet(request: RiskAssessWalletRequest):
        payload = _request_dict(request)
        chain = _normalize_chain(request.chain)
        if chain not in {"tron", "ethereum", "bitcoin"}:
            raise HTTPException(status_code=400, detail="Поддерживаются только сети: tron, ethereum, bitcoin")
        address = _validate_risk_address(chain, request.address)

        payload["chain"] = chain
        payload["address"] = address
        async with AsyncSessionLocal() as session:
            return json_safe(
                await assess_wallet_risk_pipeline(
                    session,
                    collect_wallet_report=collect_wallet_report,
                    trace_wallet_graph=trace_wallet_graph,
                    request_payload=payload,
                )
            )

    return router
