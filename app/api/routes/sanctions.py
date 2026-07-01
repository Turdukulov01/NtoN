from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.application.sanctions.screening_service import result_to_dict, screen_subject
from app.core.database import AsyncSessionLocal
from app.domain.sanctions.schemas import SanctionsScreeningInput


router = APIRouter()


class SanctionsScreenRequest(BaseModel):
    checked_subject_type: str = Field(..., examples=["client", "ubo", "director", "wallet_counterparty"])
    name: str | None = None
    aliases: list[str] = Field(default_factory=list)
    date_of_birth: str | None = None
    nationality: str | None = None
    address: str | None = None
    passport_numbers: list[str] = Field(default_factory=list)
    national_identifiers: list[str] = Field(default_factory=list)
    business_registration_numbers: list[str] = Field(default_factory=list)
    imo_numbers: list[str] = Field(default_factory=list)
    unique_id: str | None = None
    ofsi_group_id: str | None = None
    un_reference_id: str | None = None


def _request_dict(request: BaseModel) -> dict[str, Any]:
    if hasattr(request, "model_dump"):
        return request.model_dump()
    return request.dict()


@router.post("/api/sanctions/screen")
@router.post("/sanctions/screen")
async def sanctions_screen(
    request: SanctionsScreenRequest,
    limit: int = Query(25, ge=1, le=100),
    persist_matches: bool = Query(False),
):
    screening_input = SanctionsScreeningInput(**_request_dict(request))
    async with AsyncSessionLocal() as session:
        result = await screen_subject(
            session,
            screening_input,
            limit=limit,
            persist_matches=persist_matches,
        )
    return result_to_dict(result)
