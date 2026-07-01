from __future__ import annotations

from dataclasses import asdict
from typing import Iterable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.sanctions.normalization import normalize_identifier, normalize_name
from app.domain.sanctions.schemas import SanctionsScreeningInput
from app.domain.sanctions.screening import build_screening_result, decide_match
from app.models import (
    SanctionsAddress,
    SanctionsDocument,
    SanctionsMatch,
    SanctionsMatchLevelEnum,
    SanctionsName,
    SanctionsReviewStatusEnum,
    SanctionsSubject,
)


def _identifier_values(screening_input: SanctionsScreeningInput) -> set[str]:
    values = {
        screening_input.unique_id,
        screening_input.ofsi_group_id,
        screening_input.un_reference_id,
        *screening_input.passport_numbers,
        *screening_input.national_identifiers,
        *screening_input.business_registration_numbers,
        *screening_input.imo_numbers,
    }
    return {normalize_identifier(value) for value in values if normalize_identifier(value)}


def _name_values(screening_input: SanctionsScreeningInput) -> set[str]:
    values = {screening_input.name, *screening_input.aliases}
    return {normalize_name(value) for value in values if normalize_name(value)}


async def _candidate_subject_ids(session: AsyncSession, screening_input: SanctionsScreeningInput, limit: int) -> list[str]:
    subject_ids: list[str] = []
    seen: set[str] = set()

    identifiers = _identifier_values(screening_input)
    if identifiers:
        rows = await session.execute(
            select(SanctionsSubject.id)
            .outerjoin(SanctionsDocument)
            .where(
                or_(
                    SanctionsSubject.unique_id.in_(identifiers),
                    SanctionsSubject.ofsi_group_id.in_(identifiers),
                    SanctionsSubject.un_reference_id.in_(identifiers),
                    SanctionsDocument.normalized_value.in_(identifiers),
                )
            )
            .limit(limit)
        )
        for subject_id in rows.scalars():
            key = str(subject_id)
            if key not in seen:
                seen.add(key)
                subject_ids.append(key)

    names = _name_values(screening_input)
    if names and len(subject_ids) < limit:
        rows = await session.execute(
            select(SanctionsSubject.id)
            .join(SanctionsName)
            .where(SanctionsName.normalized_value.in_(names))
            .limit(limit)
        )
        for subject_id in rows.scalars():
            key = str(subject_id)
            if key not in seen:
                seen.add(key)
                subject_ids.append(key)

    if names and len(subject_ids) < limit:
        prefixes = {name[:8] for name in names if len(name) >= 8}
        if prefixes:
            prefix_filters = [SanctionsName.normalized_value.like(f"{prefix}%") for prefix in prefixes]
            rows = await session.execute(
                select(SanctionsSubject.id)
                .join(SanctionsName)
                .where(or_(*prefix_filters))
                .limit(limit - len(subject_ids))
            )
            for subject_id in rows.scalars():
                key = str(subject_id)
                if key not in seen:
                    seen.add(key)
                    subject_ids.append(key)

    return subject_ids[:limit]


async def _load_subject_bundle(session: AsyncSession, subject_id: str) -> tuple[SanctionsSubject, list[SanctionsName], list[SanctionsDocument], list[SanctionsAddress]]:
    subject = await session.get(SanctionsSubject, subject_id)
    if subject is None:
        raise LookupError(subject_id)
    names = (await session.execute(select(SanctionsName).where(SanctionsName.subject_id == subject.id))).scalars().all()
    documents = (await session.execute(select(SanctionsDocument).where(SanctionsDocument.subject_id == subject.id))).scalars().all()
    addresses = (await session.execute(select(SanctionsAddress).where(SanctionsAddress.subject_id == subject.id))).scalars().all()
    return subject, list(names), list(documents), list(addresses)


def _candidate_nationalities(subject: SanctionsSubject) -> list[str]:
    payload = subject.source_payload or {}
    individual = (((payload.get("IndividualDetails") or {}).get("Individual") or {}) if isinstance(payload, dict) else {})
    nationalities = ((individual.get("Nationalities") or {}).get("Nationality") if isinstance(individual, dict) else None)
    if isinstance(nationalities, list):
        return [str(item) for item in nationalities if item]
    if nationalities:
        return [str(nationalities)]
    return []


def _candidate_dobs(subject: SanctionsSubject) -> list[str]:
    payload = subject.source_payload or {}
    individual = (((payload.get("IndividualDetails") or {}).get("Individual") or {}) if isinstance(payload, dict) else {})
    dobs = ((individual.get("DOBs") or {}).get("DOB") if isinstance(individual, dict) else None)
    if isinstance(dobs, list):
        return [str(item) for item in dobs if item]
    if dobs:
        return [str(dobs)]
    return []


async def screen_subject(
    session: AsyncSession,
    screening_input: SanctionsScreeningInput,
    *,
    limit: int = 25,
    persist_matches: bool = False,
):
    candidate_ids = await _candidate_subject_ids(session, screening_input, limit)
    matches = []

    for subject_id in candidate_ids:
        subject, names, documents, addresses = await _load_subject_bundle(session, subject_id)
        match = decide_match(
            screening_input=screening_input,
            subject_id=str(subject.id),
            subject_unique_id=subject.unique_id,
            primary_name=subject.primary_name,
            source=subject.source.value,
            list_name=subject.list_name,
            program=subject.program,
            candidate_names=[name.value for name in names],
            candidate_aliases=[name.value for name in names if name.name_type.value != "primary"],
            candidate_documents=[document.value for document in documents],
            candidate_addresses=[address.full_text for address in addresses],
            candidate_nationalities=_candidate_nationalities(subject),
            candidate_dates_of_birth=_candidate_dobs(subject),
            candidate_ofsi_group_id=subject.ofsi_group_id,
            candidate_un_reference_id=subject.un_reference_id,
        )
        if match:
            matches.append(match)

    result = build_screening_result(matches)

    if persist_matches and result.matches:
        checked_value = screening_input.name or screening_input.unique_id or screening_input.ofsi_group_id or ""
        for match in result.matches:
            if not match.subject_id:
                continue
            session.add(
                SanctionsMatch(
                    checked_subject_type=screening_input.checked_subject_type,
                    checked_subject_value=checked_value,
                    normalized_checked_value=normalize_name(checked_value),
                    sanctions_subject_id=match.subject_id,
                    match_score=match.match_score,
                    match_level=SanctionsMatchLevelEnum(match.match_level.value),
                    matched_fields=match.matched_fields,
                    override_hit=match.override_hit,
                    review_status=SanctionsReviewStatusEnum(match.review_status.value),
                    evidence=match.evidence or asdict(match),
                )
            )
        await session.commit()

    return result


def result_to_dict(result) -> dict:
    return {
        "override_hit": result.override_hit,
        "risk_tag": result.risk_tag,
        "score": result.score,
        "risk_zone": result.risk_zone,
        "manual_review": result.manual_review,
        "matches": [
            {
                **asdict(match),
                "match_level": match.match_level.value,
                "review_status": match.review_status.value,
            }
            for match in result.matches
        ],
    }
