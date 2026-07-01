from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.application.sanctions.screening_service import result_to_dict, screen_subject
from app.core.settings import KYT_PROVIDER_CACHE_TTL_SECONDS
from app.domain.risk.engine import assess_wallet_risk, load_risk_model, score_zone
from app.domain.risk.kyt import build_external_kyt_metrics
from app.domain.sanctions.schemas import SanctionsScreeningInput
from app.infrastructure.kyt.ranex import RanexKytError, fetch_ranex_screening, normalize_ranex_screening
from app.infrastructure.kyt.shard import ShardKytError, fetch_shard_address_risk, normalize_shard_address_risk
from app.models import (
    AddressRiskTag,
    AddressRiskTagEvent,
    KytExposure,
    KytProviderReport,
    RiskAssessment,
    RiskAssessmentEvidence,
)


CollectWalletReport = Callable[..., Awaitable[dict[str, Any]]]
TraceWalletGraph = Callable[..., Awaitable[dict[str, Any]]]


def normalize_wallet_address(value: Any) -> str:
    return str(value or "").strip().lower()


def json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    return value


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set)):
        return list(value)
    return [value]


def tagged_addresses_to_rows(value: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not value:
        return rows

    if isinstance(value, dict):
        for address, payload in value.items():
            if isinstance(payload, dict):
                rows.append({"address": address, **payload})
            elif isinstance(payload, list):
                rows.append({"address": address, "tags": payload})
            else:
                rows.append({"address": address, "tag": payload})
        return rows

    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item.get("address"):
                rows.append(dict(item))

    return rows


def _addresses_from_wallet_data(
    *,
    root_address: str,
    wallet_report: dict[str, Any],
    trace_result: dict[str, Any] | None,
) -> list[str]:
    addresses: dict[str, str] = {}

    def add(value: Any) -> None:
        raw = str(value or "").strip()
        key = normalize_wallet_address(raw)
        if key:
            addresses[key] = raw

    add(root_address)
    add(wallet_report.get("address"))

    for counterparty in wallet_report.get("counterparties") or []:
        if isinstance(counterparty, dict):
            add(counterparty.get("address"))

    for tx in wallet_report.get("transactions") or []:
        if isinstance(tx, dict):
            add(tx.get("from_address"))
            add(tx.get("to_address"))

    if trace_result:
        for node in trace_result.get("nodes") or []:
            if isinstance(node, dict):
                add(node.get("address"))
        for edge in trace_result.get("edges") or []:
            if isinstance(edge, dict):
                add(edge.get("from_address"))
                add(edge.get("to_address"))

    return list(addresses.values())


async def load_address_risk_tag_rows(
    session: AsyncSession,
    *,
    network: str,
    addresses: list[str],
) -> list[dict[str, Any]]:
    normalized = {normalize_wallet_address(address) for address in addresses if normalize_wallet_address(address)}
    if not normalized:
        return []

    rows = await session.execute(
        select(AddressRiskTag).where(
            AddressRiskTag.is_active.is_(True),
            AddressRiskTag.normalized_address.in_(normalized),
            AddressRiskTag.network.in_([network, "all", "*"]),
        )
    )

    result: list[dict[str, Any]] = []
    for tag in rows.scalars():
        metadata = tag.metadata_json or {}
        row: dict[str, Any] = {
            "address": tag.address,
            "tag": tag.tag,
            "source": f"db.address_risk_tags:{tag.source}",
            "note": tag.note or tag.label or "",
            "confidence": _as_float(tag.confidence, 0.0) if tag.confidence is not None else None,
            "metadata": metadata,
        }
        if isinstance(metadata, dict) and metadata.get("score") is not None:
            row["score"] = metadata.get("score")
        result.append(row)

    return result


def _merge_tagged_addresses(request_rows: Any, db_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = tagged_addresses_to_rows(request_rows)
    merged.extend(db_rows)
    return merged


def _value_from_any(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _screening_input_from_payload(raw: dict[str, Any], *, default_type: str) -> SanctionsScreeningInput | None:
    checked_subject_type = str(raw.get("checked_subject_type") or raw.get("subject_type") or default_type)
    data = {
        "checked_subject_type": checked_subject_type,
        "name": _value_from_any(raw, "name", "full_name", "legal_name", "primary_name"),
        "aliases": [str(item) for item in _as_list(_value_from_any(raw, "aliases", "alias_names", "alias")) if str(item).strip()],
        "date_of_birth": _value_from_any(raw, "date_of_birth", "dob", "birth_date"),
        "nationality": _value_from_any(raw, "nationality", "country"),
        "address": _value_from_any(raw, "address", "registered_address", "residential_address"),
        "passport_numbers": [str(item) for item in _as_list(_value_from_any(raw, "passport_numbers", "passports", "passport_number")) if str(item).strip()],
        "national_identifiers": [
            str(item)
            for item in _as_list(_value_from_any(raw, "national_identifiers", "national_ids", "national_id"))
            if str(item).strip()
        ],
        "business_registration_numbers": [
            str(item)
            for item in _as_list(_value_from_any(raw, "business_registration_numbers", "registration_numbers", "registration_number"))
            if str(item).strip()
        ],
        "imo_numbers": [str(item) for item in _as_list(_value_from_any(raw, "imo_numbers", "imo_number")) if str(item).strip()],
        "unique_id": _value_from_any(raw, "unique_id", "sanctions_unique_id"),
        "ofsi_group_id": _value_from_any(raw, "ofsi_group_id"),
        "un_reference_id": _value_from_any(raw, "un_reference_id", "un_ref", "un_reference_number"),
    }

    has_value = any(
        data.get(key)
        for key in (
            "name",
            "date_of_birth",
            "nationality",
            "address",
            "passport_numbers",
            "national_identifiers",
            "business_registration_numbers",
            "imo_numbers",
            "unique_id",
            "ofsi_group_id",
            "un_reference_id",
        )
    )
    if not has_value:
        return None
    return SanctionsScreeningInput(**data)


def build_sanctions_screening_inputs(context: dict[str, Any]) -> list[SanctionsScreeningInput]:
    inputs: list[SanctionsScreeningInput] = []

    for raw in context.get("screening_subjects") or []:
        if isinstance(raw, dict):
            item = _screening_input_from_payload(raw, default_type="related_party")
            if item:
                inputs.append(item)

    participant_profile = context.get("participant_profile") or {}
    if isinstance(participant_profile, dict):
        item = _screening_input_from_payload(participant_profile, default_type="client")
        if item:
            inputs.append(item)

        for key, subject_type in (
            ("ubos", "ubo"),
            ("ubo", "ubo"),
            ("directors", "director"),
            ("director", "director"),
            ("beneficiaries", "beneficiary"),
            ("related_companies", "related_company"),
            ("counterparties", "counterparty"),
        ):
            values = participant_profile.get(key)
            for raw in _as_list(values):
                if isinstance(raw, dict):
                    related = _screening_input_from_payload(raw, default_type=subject_type)
                    if related:
                        inputs.append(related)

    deduped: dict[tuple[Any, ...], SanctionsScreeningInput] = {}
    for item in inputs:
        key = (
            item.checked_subject_type,
            item.name,
            tuple(item.aliases),
            item.date_of_birth,
            item.nationality,
            item.address,
            tuple(item.passport_numbers),
            tuple(item.national_identifiers),
            tuple(item.business_registration_numbers),
            tuple(item.imo_numbers),
            item.unique_id,
            item.ofsi_group_id,
            item.un_reference_id,
        )
        deduped[key] = item
    return list(deduped.values())


async def run_sanctions_screening(
    session: AsyncSession,
    *,
    context: dict[str, Any],
    limit: int = 25,
) -> dict[str, Any] | None:
    inputs = build_sanctions_screening_inputs(context)
    if not inputs:
        return None

    subject_results: list[dict[str, Any]] = []
    all_matches: list[dict[str, Any]] = []
    override_hit = False
    manual_review = False

    for screening_input in inputs:
        result = await screen_subject(session, screening_input, limit=limit, persist_matches=False)
        payload = result_to_dict(result)
        payload["checked_subject_type"] = screening_input.checked_subject_type
        payload["checked_subject_value"] = (
            screening_input.name
            or screening_input.unique_id
            or screening_input.ofsi_group_id
            or screening_input.un_reference_id
            or screening_input.address
            or ""
        )
        subject_results.append(payload)
        all_matches.extend(payload.get("matches") or [])
        override_hit = override_hit or bool(payload.get("override_hit"))
        manual_review = manual_review or bool(payload.get("manual_review"))

    if not all_matches:
        risk_zone = "NO_MATCH"
    elif override_hit:
        risk_zone = "RED"
    else:
        risk_zone = "MANUAL_REVIEW"

    return {
        "override_hit": override_hit,
        "risk_tag": "sanction" if all_matches else "",
        "score": 100 if override_hit else 0,
        "risk_zone": risk_zone,
        "manual_review": manual_review and not override_hit,
        "subjects_checked": len(inputs),
        "subjects": subject_results,
        "matches": all_matches[:50],
    }


def _wallet_report_snapshot(wallet_report: dict[str, Any]) -> dict[str, Any]:
    return {
        "address": wallet_report.get("address"),
        "network": wallet_report.get("network"),
        "counts": wallet_report.get("counts"),
        "summary": wallet_report.get("summary"),
        "turnover": wallet_report.get("turnover"),
        "frequency": wallet_report.get("frequency"),
        "suspicious_patterns": (wallet_report.get("suspicious_patterns") or [])[:50],
        "counterparties": (wallet_report.get("counterparties") or [])[:100],
    }


def _trace_snapshot(trace_result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not trace_result:
        return None
    return {
        "nodes": (trace_result.get("nodes") or [])[:250],
        "edges": (trace_result.get("edges") or [])[:500],
        "summary": trace_result.get("summary"),
        "meta": trace_result.get("meta"),
    }


def _wallet_report_from_external_kyt(
    *,
    network: str,
    address: str,
    external_kyt: dict[str, Any],
) -> dict[str, Any]:
    transactions = external_kyt.get("transactions") if isinstance(external_kyt.get("transactions"), dict) else {}
    balances = external_kyt.get("balances") if isinstance(external_kyt.get("balances"), dict) else {}
    tokens = balances.get("tokens") if isinstance(balances, dict) and isinstance(balances.get("tokens"), list) else []
    activity = external_kyt.get("activity") if isinstance(external_kyt.get("activity"), dict) else {}

    return {
        "network": external_kyt.get("network") or network,
        "address": external_kyt.get("address") or address,
        "source": external_kyt.get("source") or external_kyt.get("provider") or "external_kyt",
        "period": {
            "all_time": True,
            "source": "external_kyt",
            "first": activity.get("first"),
            "last": activity.get("last"),
        },
        "counts": {
            "total": _as_float(transactions.get("total"), 0),
            "sent": _as_float(transactions.get("sent"), 0),
            "received": _as_float(transactions.get("received"), 0),
            "external_kyt": True,
        },
        "summary": [],
        "turnover": {
            "assets": [],
            "unique_counterparties": 0,
            "rapid_transit": {"count": 0},
            "external_kyt_total": external_kyt.get("total"),
            "external_kyt_total_human": external_kyt.get("total_human"),
        },
        "frequency": {},
        "counterparties": [],
        "transactions": [],
        "normalized_transactions": [],
        "suspicious_patterns": [],
        "balances": tokens,
        "data_source_note": "wallet report synthesized from external KYT provider; raw on-chain sample was skipped",
    }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _datetime_from_unix(value: Any) -> datetime | None:
    timestamp = _as_float(value, 0)
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def _datetime_from_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _request_payload_snapshot(request_payload: dict[str, Any]) -> dict[str, Any]:
    snapshot = dict(request_payload)
    raw = snapshot.get("external_kyt_raw")
    if isinstance(raw, dict):
        snapshot["external_kyt_raw"] = {
            "provider": raw.get("provider"),
            "source": raw.get("source"),
            "address": raw.get("address"),
            "network": raw.get("network"),
            "stored_in_evidence": True,
        }
    return snapshot


def _provider_report_external_kyt_payload(report: KytProviderReport, *, cache_status: str) -> dict[str, Any]:
    payload = dict(report.normalized_payload or {})
    payload["provider_report_id"] = str(report.id)
    payload["cache_status"] = cache_status
    payload["fetched_at"] = report.fetched_at.isoformat() if report.fetched_at else None
    payload["expires_at"] = report.expires_at.isoformat() if report.expires_at else None
    return payload


async def load_fresh_kyt_provider_report(
    session: AsyncSession,
    *,
    provider: str,
    network: str,
    address: str,
) -> KytProviderReport | None:
    now = _utcnow()
    normalized = normalize_wallet_address(address)
    result = await session.execute(
        select(KytProviderReport)
        .where(
            KytProviderReport.provider == provider,
            KytProviderReport.network == network,
            KytProviderReport.normalized_address == normalized,
            KytProviderReport.expires_at > now,
        )
        .order_by(KytProviderReport.fetched_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def save_kyt_provider_report(
    session: AsyncSession,
    *,
    provider: str,
    network: str,
    address: str,
    raw_response: dict[str, Any],
    normalized_payload: dict[str, Any],
) -> KytProviderReport:
    fetched_at = _utcnow()
    expires_at = fetched_at + timedelta(seconds=max(60, int(KYT_PROVIDER_CACHE_TTL_SECONDS)))
    normalized_address = normalize_wallet_address(address)
    activity = normalized_payload.get("activity") if isinstance(normalized_payload.get("activity"), dict) else {}
    transactions = normalized_payload.get("transactions") if isinstance(normalized_payload.get("transactions"), dict) else {}

    report = (
        await session.execute(
            select(KytProviderReport).where(
                KytProviderReport.provider == provider,
                KytProviderReport.network == network,
                KytProviderReport.normalized_address == normalized_address,
            )
        )
    ).scalar_one_or_none()

    if report is None:
        report = KytProviderReport(
            provider=provider,
            network=network,
            normalized_address=normalized_address,
            created_at=fetched_at,
        )
        session.add(report)

    report.address = normalized_payload.get("address") or address
    report.provider_score = _as_float(normalized_payload.get("risk_score"), 0)
    report.provider_risk_level = normalized_payload.get("risk_zone") or (normalized_payload.get("provider_reported") or {}).get("name")
    report.tx_total = int(_as_float(transactions.get("total"), 0)) if transactions.get("total") not in (None, "") else None
    report.activity_first = _datetime_from_unix(activity.get("first_timestamp")) or _datetime_from_iso(activity.get("first"))
    report.activity_last = _datetime_from_unix(activity.get("last_timestamp")) or _datetime_from_iso(activity.get("last"))
    report.raw_response = json_safe(raw_response)
    report.normalized_payload = json_safe(normalized_payload)
    report.fetched_at = fetched_at
    report.expires_at = expires_at
    await session.flush()

    await session.execute(delete(KytExposure).where(KytExposure.report_id == report.id))
    model = load_risk_model()
    metrics = build_external_kyt_metrics(normalized_payload, model)
    for exposure in metrics.get("external_kyt_exposures") or []:
        session.add(
            KytExposure(
                report_id=report.id,
                provider=provider,
                network=network,
                address=report.address,
                normalized_address=normalized_address,
                category_name=exposure.get("name") or "",
                category_key=exposure.get("category_key") or "",
                exposure_group=exposure.get("group"),
                percent=_as_float(exposure.get("percent"), 0),
                amount=exposure.get("amount"),
                amount_human=exposure.get("amount_human"),
                provider_risk_score=_as_float(exposure.get("provider_risk_score"), 0),
                model_category_score=_as_float(exposure.get("model_category_score"), 0),
                model_contribution_score=_as_float(exposure.get("model_contribution_score"), 0),
                payload=json_safe(exposure),
                created_at=fetched_at,
            )
        )
    await session.flush()
    return report


def normalize_kyt_provider(value: Any) -> str:
    key = str(value or "").strip().lower()
    if key in {"", "none", "off", "manual", "internal"}:
        return ""
    if key in {"ranex", "ranex_model", "ranex_provider"}:
        return "ranex"
    if key in {"shard", "shard_model", "shard_provider"}:
        return "shard"
    return key


def _kyt_token_from_context(context: dict[str, Any]) -> str | None:
    wallet_profile = context.get("wallet_profile") if isinstance(context.get("wallet_profile"), dict) else {}
    for value in (
        context.get("kyt_token"),
        context.get("external_kyt_token"),
        wallet_profile.get("kyt_token"),
        wallet_profile.get("token"),
        wallet_profile.get("asset"),
    ):
        if value not in (None, ""):
            return str(value).strip().lower()
    return None


async def load_external_kyt_context(
    session: AsyncSession,
    *,
    context: dict[str, Any],
    network: str,
    address: str,
) -> None:
    existing = context.get("external_kyt")
    if isinstance(existing, dict) and existing:
        return

    provider = normalize_kyt_provider(context.get("kyt_provider") or context.get("external_kyt_provider"))
    if provider:
        context["kyt_provider"] = provider
    if not provider:
        return

    if provider not in {"ranex", "shard"}:
        message = f"Unsupported KYT provider: {provider}"
        if context.get("external_kyt_required"):
            raise HTTPException(status_code=400, detail=message)
        context["external_kyt_error"] = message
        return

    if not context.get("force_kyt_refresh"):
        cached_report = await load_fresh_kyt_provider_report(
            session,
            provider=provider,
            network=network,
            address=address,
        )
        if cached_report:
            context["external_kyt"] = _provider_report_external_kyt_payload(cached_report, cache_status="cache")
            context["external_kyt_cache_status"] = "cache"
            context["external_kyt_provider_report_id"] = str(cached_report.id)
            return

    try:
        if provider == "ranex":
            raw_response = await fetch_ranex_screening(network=network, address=address)
            normalized = normalize_ranex_screening(raw_response)
        else:
            token = _kyt_token_from_context(context)
            raw_response = await fetch_shard_address_risk(network=network, address=address, token=token)
            normalized = normalize_shard_address_risk(raw_response, network=network, address=address, token=token)
    except (RanexKytError, ShardKytError) as exc:
        message = str(exc)
        if context.get("external_kyt_required"):
            raise HTTPException(status_code=502, detail=message) from exc
        context["external_kyt_error"] = message
        return

    report = await save_kyt_provider_report(
        session,
        provider=provider,
        network=network,
        address=address,
        raw_response=raw_response,
        normalized_payload=normalized,
    )
    context["external_kyt"] = _provider_report_external_kyt_payload(report, cache_status="fresh")
    context["external_kyt_cache_status"] = "fresh"
    context["external_kyt_provider_report_id"] = str(report.id)
    context["external_kyt_cache_saved"] = True


async def save_risk_assessment(
    session: AsyncSession,
    *,
    network: str,
    address: str,
    request_payload: dict[str, Any],
    wallet_report: dict[str, Any],
    trace_result: dict[str, Any] | None,
    result: dict[str, Any],
    sanctions_screening: dict[str, Any] | None,
    db_address_tags: list[dict[str, Any]],
) -> str:
    assessment = RiskAssessment(
        network=network,
        address=address,
        normalized_address=normalize_wallet_address(address),
        model_version=result.get("model_version"),
        raw_score=_as_float(result.get("raw_score")),
        final_score=_as_float(result.get("final_score")),
        risk_zone=str(result.get("risk_zone") or "UNKNOWN"),
        category=result.get("category"),
        override_hit=bool(result.get("override_hit")),
        override_reasons=json_safe(result.get("override_reasons") or []),
        request_payload=json_safe(_request_payload_snapshot(request_payload)),
        wallet_report=json_safe(_wallet_report_snapshot(wallet_report)),
        trace_result=json_safe(_trace_snapshot(trace_result)),
        sanctions_screening=json_safe(sanctions_screening),
        result_payload=json_safe(result),
        data_quality=json_safe(result.get("data_quality") or {}),
    )
    session.add(assessment)
    await session.flush()

    for tag_row in db_address_tags:
        session.add(
            RiskAssessmentEvidence(
                assessment_id=assessment.id,
                evidence_type="address_risk_tag",
                source=tag_row.get("source"),
                subject_type="wallet_address",
                subject_value=tag_row.get("address"),
                payload=json_safe(tag_row),
            )
        )

    if sanctions_screening:
        for subject in sanctions_screening.get("subjects") or []:
            session.add(
                RiskAssessmentEvidence(
                    assessment_id=assessment.id,
                    evidence_type="sanctions_screening",
                    source="sanctions_screening",
                    subject_type=subject.get("checked_subject_type"),
                    subject_value=subject.get("checked_subject_value"),
                    payload=json_safe(subject),
                )
            )

    external_kyt = request_payload.get("external_kyt")
    if isinstance(external_kyt, dict) and external_kyt:
        session.add(
            RiskAssessmentEvidence(
                assessment_id=assessment.id,
                evidence_type="external_kyt_report",
                source=str(external_kyt.get("source") or "external_kyt"),
                subject_type="wallet_address",
                subject_value=str(external_kyt.get("address") or address),
                payload=json_safe(external_kyt),
            )
        )

    external_kyt_raw = request_payload.get("external_kyt_raw")
    if isinstance(external_kyt_raw, dict) and external_kyt_raw:
        session.add(
            RiskAssessmentEvidence(
                assessment_id=assessment.id,
                evidence_type="external_kyt_raw_response",
                source=str(external_kyt_raw.get("source") or external_kyt_raw.get("provider") or "external_kyt"),
                subject_type="wallet_address",
                subject_value=str(external_kyt_raw.get("address") or address),
                payload=json_safe(external_kyt_raw),
            )
        )

    for exposure in result.get("risk_exposures") or []:
        session.add(
            RiskAssessmentEvidence(
                assessment_id=assessment.id,
                evidence_type="risk_exposure",
                source=exposure.get("source"),
                subject_type="wallet_address",
                subject_value=exposure.get("address"),
                payload=json_safe(exposure),
            )
        )

    await session.commit()
    return str(assessment.id)


def _normalized_score_mode(context: dict[str, Any]) -> str:
    mode = str(context.get("score_mode") or "model").strip().lower()
    aliases = {
        "external": "provider",
        "external_provider": "provider",
        "provider_score": "provider",
        "ranex": "provider",
        "ranex_provider": "provider",
        "shard": "provider",
        "shard_provider": "provider",
    }
    return aliases.get(mode, mode)


def _external_provider_score(external_kyt: dict[str, Any]) -> float | None:
    for key in ("risk_score", "provider_risk_score", "score", "risk_percent", "risk_percentage"):
        value = external_kyt.get(key)
        if value not in (None, ""):
            return _as_float(value)
    return None


def _apply_provider_score_mode(result: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if _normalized_score_mode(context) != "provider":
        result["score_source"] = "model"
        result["provider_score_mode"] = False
        data_quality = dict(result.get("data_quality") or {})
        data_quality.setdefault("score_source", "model")
        data_quality.setdefault("provider_score_mode", False)
        result["data_quality"] = data_quality
        return result

    external_kyt = context.get("external_kyt") if isinstance(context.get("external_kyt"), dict) else {}
    if not external_kyt:
        raise HTTPException(status_code=502, detail="Для режима 'Только скоринг провайдера' нет ответа KYT-провайдера")

    provider_score = _external_provider_score(external_kyt)
    if provider_score is None:
        raise HTTPException(status_code=502, detail="KYT-провайдер не вернул risk score")

    model = load_risk_model()
    provider_score = round(max(0.0, min(100.0, provider_score)), 2)
    zone_code, zone = score_zone(provider_score, model)
    recommended_actions = list(zone.get("recommended_actions") or [])
    action_labels = model.get("action_labels") or {}

    model_result = {
        "raw_score": result.get("raw_score"),
        "final_score": result.get("final_score"),
        "risk_zone": result.get("risk_zone"),
        "category": result.get("category"),
        "zone_label": result.get("zone_label"),
        "override_hit": result.get("override_hit"),
        "override_reasons": result.get("override_reasons") or [],
        "policy_adjustments": result.get("policy_adjustments") or [],
        "blocks": result.get("blocks") or {},
        "block_details": result.get("block_details") or {},
    }

    data_quality = dict(result.get("data_quality") or {})
    data_quality.update(
        {
            "score_source": "external_provider",
            "provider_score_mode": True,
            "model_score_kept_as_diagnostic": True,
        }
    )

    result.update(
        {
            "raw_score": provider_score,
            "final_score": provider_score,
            "risk_zone": zone_code,
            "category": zone.get("category") or "unknown",
            "zone_label": zone.get("label") or zone_code,
            "monitoring_frequency": zone.get("monitoring_frequency"),
            "override_hit": False,
            "override_reasons": [],
            "override_details": [],
            "policy_adjustments": [],
            "recommended_actions": recommended_actions,
            "recommended_action_labels": [action_labels.get(action, action) for action in recommended_actions],
            "score_source": "external_provider",
            "score_provider": external_kyt.get("provider") or external_kyt.get("source") or context.get("kyt_provider") or "external_kyt",
            "provider_score_mode": True,
            "model_result": model_result,
            "data_quality": data_quality,
        }
    )
    return result


async def assess_wallet_risk_pipeline(
    session: AsyncSession,
    *,
    collect_wallet_report: CollectWalletReport,
    trace_wallet_graph: TraceWalletGraph,
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    context = dict(request_payload)
    network = str(context.get("chain") or context.get("network") or "tron").strip().lower()
    address = str(context.get("address") or "").strip()
    skip_wallet_collection = bool(context.get("skip_wallet_collection"))
    external_provider = normalize_kyt_provider(context.get("kyt_provider") or context.get("external_kyt_provider"))
    if external_provider:
        context["kyt_provider"] = external_provider

    if skip_wallet_collection and external_provider:
        await load_external_kyt_context(session, context=context, network=network, address=address)
        external_kyt = context.get("external_kyt") if isinstance(context.get("external_kyt"), dict) else {}
        if external_kyt:
            report = _wallet_report_from_external_kyt(network=network, address=address, external_kyt=external_kyt)
        else:
            report = await collect_wallet_report(
                network=network,
                address=address,
                days=context.get("days", 0),
                max_items=context.get("max_items", 500),
                all_items=context.get("all_items", True),
                date_from=context.get("date_from"),
                date_to=context.get("date_to"),
            )
    else:
        report = await collect_wallet_report(
            network=network,
            address=address,
            days=context.get("days", 0),
            max_items=context.get("max_items", 500),
            all_items=context.get("all_items", True),
            date_from=context.get("date_from"),
            date_to=context.get("date_to"),
        )

    trace_result = None
    if context.get("include_trace", True) and network == "tron" and not skip_wallet_collection:
        trace_result = await trace_wallet_graph(
            address=report.get("address") or address,
            days=context.get("days", 0),
            depth=context.get("trace_depth", 1),
            asset="all",
            include_incoming=True,
            max_branches=context.get("trace_max_branches", 25),
            max_items_per_wallet=context.get("trace_max_items_per_wallet", 200),
            date_from=context.get("date_from"),
            date_to=context.get("date_to"),
        )

    address = str(report.get("address") or address).strip()
    addresses = _addresses_from_wallet_data(root_address=address, wallet_report=report, trace_result=trace_result)
    db_address_tags = await load_address_risk_tag_rows(session, network=network, addresses=addresses)

    context["chain"] = network
    context["address"] = address
    context["tagged_addresses"] = _merge_tagged_addresses(context.get("tagged_addresses"), db_address_tags)
    context["wallet_collection_skipped"] = bool(skip_wallet_collection and context.get("external_kyt"))

    await load_external_kyt_context(session, context=context, network=network, address=address)

    sanctions_screening = await run_sanctions_screening(
        session,
        context=context,
        limit=int(context.get("sanctions_screening_limit") or 25),
    )
    if sanctions_screening:
        context["sanctions_screening"] = sanctions_screening

    result = assess_wallet_risk(report, trace_result=trace_result, context=context)
    result = _apply_provider_score_mode(result, context)
    result["sanctions_screening"] = sanctions_screening
    result["address_risk_tags_loaded"] = len(db_address_tags)

    if context.get("persist_assessment", True):
        assessment_id = await save_risk_assessment(
            session,
            network=network,
            address=address,
            request_payload=context,
            wallet_report=report,
            trace_result=trace_result,
            result=result,
            sanctions_screening=sanctions_screening,
            db_address_tags=db_address_tags,
        )
        result["assessment_id"] = assessment_id
    elif context.get("external_kyt_cache_saved"):
        await session.commit()

    return result


async def upsert_address_risk_tag(
    session: AsyncSession,
    *,
    network: str,
    address: str,
    tag: str,
    source: str = "manual",
    label: str | None = None,
    confidence: float | None = None,
    note: str | None = None,
    metadata_json: dict[str, Any] | None = None,
    is_active: bool = True,
    actor: str | None = "api",
) -> AddressRiskTag:
    normalized = normalize_wallet_address(address)
    tag_key = str(tag or "").strip().lower()
    source_key = str(source or "manual").strip().lower()
    existing = (
        await session.execute(
            select(AddressRiskTag).where(
                AddressRiskTag.network == network,
                AddressRiskTag.normalized_address == normalized,
                AddressRiskTag.tag == tag_key,
                AddressRiskTag.source == source_key,
            )
        )
    ).scalar_one_or_none()

    if existing:
        previous_payload = address_risk_tag_to_dict(existing)
        existing.address = address
        existing.label = label
        existing.confidence = confidence
        existing.note = note
        existing.metadata_json = metadata_json
        existing.is_active = is_active
        await session.flush()
        session.add(
            AddressRiskTagEvent(
                address_risk_tag_id=existing.id,
                action="update",
                actor=actor,
                previous_payload=json_safe(previous_payload),
                new_payload=json_safe(address_risk_tag_to_dict(existing)),
                created_at=_utcnow(),
            )
        )
        await session.commit()
        return existing

    row = AddressRiskTag(
        network=network,
        address=address,
        normalized_address=normalized,
        tag=tag_key,
        label=label,
        source=source_key,
        confidence=confidence,
        note=note,
        metadata_json=metadata_json,
        is_active=is_active,
    )
    session.add(row)
    await session.flush()
    session.add(
        AddressRiskTagEvent(
            address_risk_tag_id=row.id,
            action="create",
            actor=actor,
            previous_payload=None,
            new_payload=json_safe(address_risk_tag_to_dict(row)),
            created_at=_utcnow(),
        )
    )
    await session.commit()
    return row


async def delete_address_risk_tag(
    session: AsyncSession,
    *,
    tag_id: UUID,
) -> dict[str, Any] | None:
    row = await session.get(AddressRiskTag, tag_id)
    if not row:
        return None
    payload = address_risk_tag_to_dict(row)
    await session.delete(row)
    await session.commit()
    return payload


def address_risk_tag_to_dict(row: AddressRiskTag) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "network": row.network,
        "address": row.address,
        "normalized_address": row.normalized_address,
        "tag": row.tag,
        "label": row.label,
        "source": row.source,
        "confidence": float(row.confidence) if row.confidence is not None else None,
        "is_active": row.is_active,
        "note": row.note,
        "metadata_json": row.metadata_json or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def address_risk_tag_event_to_dict(row: AddressRiskTagEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        "address_risk_tag_id": str(row.address_risk_tag_id),
        "action": row.action,
        "actor": row.actor,
        "previous_payload": row.previous_payload,
        "new_payload": row.new_payload,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def list_address_risk_tags(
    session: AsyncSession,
    *,
    network: str | None = None,
    address: str | None = None,
    tag: str | None = None,
    active_only: bool = False,
    limit: int = 100,
) -> list[AddressRiskTag]:
    query = select(AddressRiskTag).order_by(AddressRiskTag.created_at.desc()).limit(limit)
    if network:
        query = query.where(AddressRiskTag.network == network)
    if address:
        query = query.where(AddressRiskTag.normalized_address == normalize_wallet_address(address))
    if tag:
        query = query.where(AddressRiskTag.tag == tag.strip().lower())
    if active_only:
        query = query.where(AddressRiskTag.is_active.is_(True))
    return list((await session.execute(query)).scalars().all())


async def list_address_risk_tag_events(
    session: AsyncSession,
    *,
    address_risk_tag_id: UUID,
    limit: int = 100,
) -> list[AddressRiskTagEvent]:
    query = (
        select(AddressRiskTagEvent)
        .where(AddressRiskTagEvent.address_risk_tag_id == address_risk_tag_id)
        .order_by(AddressRiskTagEvent.created_at.desc(), AddressRiskTagEvent.id.desc())
        .limit(limit)
    )
    return list((await session.execute(query)).scalars().all())


def kyt_exposure_to_dict(row: KytExposure) -> dict[str, Any]:
    return {
        "id": row.id,
        "report_id": str(row.report_id),
        "provider": row.provider,
        "network": row.network,
        "address": row.address,
        "category_name": row.category_name,
        "category_key": row.category_key,
        "exposure_group": row.exposure_group,
        "percent": float(row.percent) if row.percent is not None else 0.0,
        "amount": row.amount,
        "amount_human": row.amount_human,
        "provider_risk_score": float(row.provider_risk_score) if row.provider_risk_score is not None else None,
        "model_category_score": float(row.model_category_score) if row.model_category_score is not None else None,
        "model_contribution_score": float(row.model_contribution_score) if row.model_contribution_score is not None else None,
        "payload": row.payload or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def kyt_provider_report_to_dict(
    row: KytProviderReport,
    *,
    include_raw: bool = False,
    include_normalized: bool = False,
) -> dict[str, Any]:
    now = _utcnow()
    exposures = sorted(row.exposures or [], key=lambda item: _as_float(item.percent), reverse=True)
    payload = {
        "id": str(row.id),
        "provider": row.provider,
        "network": row.network,
        "address": row.address,
        "provider_score": float(row.provider_score) if row.provider_score is not None else None,
        "provider_risk_level": row.provider_risk_level,
        "tx_total": row.tx_total,
        "activity_first": row.activity_first.isoformat() if row.activity_first else None,
        "activity_last": row.activity_last.isoformat() if row.activity_last else None,
        "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "cache_status": "fresh" if row.expires_at and row.expires_at > now else "expired",
        "expires_in_seconds": int((row.expires_at - now).total_seconds()) if row.expires_at and row.expires_at > now else 0,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "exposures": [kyt_exposure_to_dict(item) for item in exposures],
    }
    if include_normalized:
        payload["normalized_payload"] = row.normalized_payload or {}
    if include_raw:
        payload["raw_response"] = row.raw_response or {}
    return payload


async def list_kyt_provider_reports(
    session: AsyncSession,
    *,
    provider: str | None = None,
    network: str | None = None,
    address: str | None = None,
    limit: int = 50,
) -> list[KytProviderReport]:
    query = (
        select(KytProviderReport)
        .options(selectinload(KytProviderReport.exposures))
        .order_by(KytProviderReport.fetched_at.desc())
        .limit(limit)
    )
    if provider:
        query = query.where(KytProviderReport.provider == provider)
    if network:
        query = query.where(KytProviderReport.network == network)
    if address:
        query = query.where(KytProviderReport.normalized_address == normalize_wallet_address(address))
    return list((await session.execute(query)).scalars().all())


async def get_kyt_provider_report(
    session: AsyncSession,
    *,
    report_id: UUID,
) -> KytProviderReport | None:
    result = await session.execute(
        select(KytProviderReport)
        .options(selectinload(KytProviderReport.exposures))
        .where(KytProviderReport.id == report_id)
    )
    return result.scalar_one_or_none()


async def delete_kyt_provider_report(
    session: AsyncSession,
    *,
    report_id: UUID,
) -> bool:
    report = await session.get(KytProviderReport, report_id)
    if report is None:
        return False
    await session.delete(report)
    await session.commit()
    return True
