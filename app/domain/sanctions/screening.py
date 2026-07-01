from __future__ import annotations

from difflib import SequenceMatcher

from app.domain.sanctions.normalization import normalize_address, normalize_identifier, normalize_name
from app.domain.sanctions.schemas import (
    SanctionsMatchLevel,
    SanctionsReviewStatus,
    SanctionsScreeningInput,
    SanctionsScreeningMatch,
    SanctionsScreeningResult,
)


CRITICAL_SCORE = 100


def fuzzy_name_score(left: str, right: str) -> float:
    left_norm = normalize_name(left)
    right_norm = normalize_name(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 100.0
    return round(SequenceMatcher(None, left_norm, right_norm).ratio() * 100, 2)


def decide_match(
    *,
    screening_input: SanctionsScreeningInput,
    subject_id: str | None,
    subject_unique_id: str,
    primary_name: str,
    source: str,
    list_name: str,
    program: str,
    candidate_names: list[str],
    candidate_documents: list[str],
    candidate_addresses: list[str],
    candidate_nationalities: list[str],
    candidate_dates_of_birth: list[str],
    candidate_ofsi_group_id: str | None = None,
    candidate_un_reference_id: str | None = None,
    candidate_aliases: list[str] | None = None,
) -> SanctionsScreeningMatch | None:
    matched_fields: list[str] = []

    input_identifiers = {
        "unique_id": normalize_identifier(screening_input.unique_id),
        "ofsi_group_id": normalize_identifier(screening_input.ofsi_group_id),
        "un_reference_id": normalize_identifier(screening_input.un_reference_id),
    }
    candidate_identifiers = {
        "unique_id": normalize_identifier(subject_unique_id),
        "ofsi_group_id": normalize_identifier(candidate_ofsi_group_id),
        "un_reference_id": normalize_identifier(candidate_un_reference_id),
    }
    for field_name, input_value in input_identifiers.items():
        if input_value and input_value == candidate_identifiers.get(field_name):
            return SanctionsScreeningMatch(
                subject_unique_id=subject_unique_id,
                subject_id=subject_id,
                primary_name=primary_name,
                source=source,
                list_name=list_name,
                program=program,
                match_score=100.0,
                match_level=SanctionsMatchLevel.EXACT,
                matched_fields=[field_name],
                override_hit=True,
                review_status=SanctionsReviewStatus.CONFIRMED,
                reason=f"Confirmed match by sanctions {field_name}",
            )

    input_documents = {
        normalize_identifier(value)
        for value in [
            *screening_input.passport_numbers,
            *screening_input.national_identifiers,
            *screening_input.business_registration_numbers,
            *screening_input.imo_numbers,
        ]
        if value
    }
    candidate_document_set = {normalize_identifier(value) for value in candidate_documents if value}
    if input_documents and input_documents.intersection(candidate_document_set):
        matched_fields.append("document")

    checked_names = [screening_input.name or ""]
    checked_names = [name for name in checked_names if normalize_name(name)]
    primary_name_scores = [
        fuzzy_name_score(checked_name, candidate_name)
        for checked_name in checked_names
        for candidate_name in candidate_names
        if checked_name and candidate_name
    ]
    candidate_aliases = candidate_aliases or []
    alias_scores = [
        fuzzy_name_score(checked_alias, candidate_alias)
        for checked_alias in screening_input.aliases
        for candidate_alias in candidate_aliases
        if normalize_name(checked_alias) and normalize_name(candidate_alias)
    ]
    primary_name_score = max(primary_name_scores, default=0.0)
    alias_score = max(alias_scores, default=0.0)
    best_name_score = max(primary_name_score, alias_score)
    if primary_name_score == 100.0:
        matched_fields.append("name")
    if alias_score == 100.0:
        matched_fields.append("alias")

    input_dob = normalize_identifier(screening_input.date_of_birth)
    candidate_dobs = {normalize_identifier(value) for value in candidate_dates_of_birth if value}
    if input_dob and input_dob in candidate_dobs:
        matched_fields.append("date_of_birth")

    input_address = normalize_address(screening_input.address)
    if input_address:
        candidate_address_scores = [
            SequenceMatcher(None, input_address, normalize_address(value)).ratio() * 100
            for value in candidate_addresses
            if normalize_address(value)
        ]
        if max(candidate_address_scores, default=0.0) >= 90:
            matched_fields.append("address")

    input_nationality = normalize_name(screening_input.nationality)
    candidate_nationality_set = {normalize_name(value) for value in candidate_nationalities if value}
    if input_nationality and input_nationality in candidate_nationality_set:
        matched_fields.append("nationality")

    hard_secondary_fields = {field for field in matched_fields if field not in {"name", "alias", "nationality"}}
    nationality_with_alias = "nationality" in matched_fields and "alias" in matched_fields
    override_secondary_fields = hard_secondary_fields or nationality_with_alias
    if primary_name_score == 100.0 and override_secondary_fields:
        return SanctionsScreeningMatch(
            subject_unique_id=subject_unique_id,
            subject_id=subject_id,
            primary_name=primary_name,
            source=source,
            list_name=list_name,
            program=program,
            match_score=100.0,
            match_level=SanctionsMatchLevel.EXACT,
            matched_fields=sorted(set(matched_fields)),
            override_hit=True,
            review_status=SanctionsReviewStatus.CONFIRMED,
            reason="Confirmed sanctions match by exact name and secondary identifier",
        )

    if primary_name_score >= 92.0 and override_secondary_fields:
        return SanctionsScreeningMatch(
            subject_unique_id=subject_unique_id,
            subject_id=subject_id,
            primary_name=primary_name,
            source=source,
            list_name=list_name,
            program=program,
            match_score=primary_name_score,
            match_level=SanctionsMatchLevel.STRONG,
            matched_fields=sorted(set(["name", *matched_fields])),
            override_hit=True,
            review_status=SanctionsReviewStatus.CONFIRMED,
            reason="Strong sanctions match by fuzzy name and secondary identifier",
        )

    if best_name_score == 100.0:
        weak_fields = ["name"] if primary_name_score == 100.0 else ["alias"]
        return SanctionsScreeningMatch(
            subject_unique_id=subject_unique_id,
            subject_id=subject_id,
            primary_name=primary_name,
            source=source,
            list_name=list_name,
            program=program,
            match_score=75.0,
            match_level=SanctionsMatchLevel.WEAK,
            matched_fields=weak_fields,
            override_hit=False,
            review_status=SanctionsReviewStatus.PENDING,
            reason="Name-only sanctions match requires manual review",
        )

    if best_name_score >= 82.0:
        return SanctionsScreeningMatch(
            subject_unique_id=subject_unique_id,
            subject_id=subject_id,
            primary_name=primary_name,
            source=source,
            list_name=list_name,
            program=program,
            match_score=best_name_score,
            match_level=SanctionsMatchLevel.WEAK,
            matched_fields=["name"],
            override_hit=False,
            review_status=SanctionsReviewStatus.PENDING,
            reason="Weak sanctions name similarity requires manual review",
        )

    if matched_fields:
        return SanctionsScreeningMatch(
            subject_unique_id=subject_unique_id,
            subject_id=subject_id,
            primary_name=primary_name,
            source=source,
            list_name=list_name,
            program=program,
            match_score=70.0,
            match_level=SanctionsMatchLevel.WEAK,
            matched_fields=sorted(set(matched_fields)),
            override_hit=False,
            review_status=SanctionsReviewStatus.PENDING,
            reason="Secondary-field sanctions similarity requires manual review",
        )

    return None


def build_screening_result(matches: list[SanctionsScreeningMatch]) -> SanctionsScreeningResult:
    sorted_matches = sorted(matches, key=lambda item: (item.override_hit, item.match_score), reverse=True)
    override_hit = any(match.override_hit for match in sorted_matches)
    return SanctionsScreeningResult(
        override_hit=override_hit,
        risk_tag="sanction" if sorted_matches else "",
        score=CRITICAL_SCORE if override_hit else 0,
        risk_zone="RED" if override_hit else "MANUAL_REVIEW" if sorted_matches else "NO_MATCH",
        matches=sorted_matches,
        manual_review=bool(sorted_matches) and not override_hit,
    )
