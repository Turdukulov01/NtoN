from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
from typing import Any
import unicodedata

import yaml

from app.domain.risk.kyt import build_external_kyt_metrics, evaluate_external_kyt_policy_adjustments


DEFAULT_MODEL_PATH = Path(__file__).resolve().parents[3] / "risk_model.yaml"


def _normalize_key(value: Any) -> str:
    text = str(value or "").strip().lower().replace("ё", "е")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^0-9a-zа-я]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_")


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    return max(minimum, min(maximum, value))


def _get_path(source: dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = source
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


@lru_cache(maxsize=8)
def load_risk_model(path: str | Path = DEFAULT_MODEL_PATH) -> dict[str, Any]:
    model_path = Path(path)
    with model_path.open("r", encoding="utf-8") as handle:
        model = yaml.safe_load(handle) or {}
    return model


def build_tag_registry(model: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    tags = model.get("risk_tags") or {}
    canonical: dict[str, dict[str, Any]] = {}
    aliases: dict[str, str] = {}

    for raw_code, raw_meta in tags.items():
        code = _normalize_key(raw_code)
        meta = dict(raw_meta or {})
        meta["code"] = code
        meta["score"] = _as_float(meta.get("score"))
        canonical[code] = meta
        aliases[code] = code
        aliases[_normalize_key(raw_code)] = code
        for alias in meta.get("aliases") or []:
            aliases[_normalize_key(alias)] = code

    return canonical, aliases


def build_jurisdiction_registry(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    data_sources = model.get("data_sources") or {}
    registry: dict[str, dict[str, Any]] = {}

    def put_row(value: Any, row: dict[str, Any]) -> None:
        key = _normalize_key(value)
        if not key:
            return
        existing = registry.get(key)
        if not existing:
            registry[key] = row
            return

        existing_score = _as_float(existing.get("score"))
        row_score = _as_float(row.get("score"))
        if row_score > existing_score:
            merged = {**row}
            if existing.get("policy_list") and not merged.get("policy_list"):
                merged["policy_list"] = existing.get("policy_list")
                merged["policy_source"] = existing.get("policy_source")
                merged["policy_source_url"] = existing.get("policy_source_url")
                merged["policy_as_of"] = existing.get("policy_as_of")
            registry[key] = merged
            return

        if row.get("policy_list") and not existing.get("policy_list"):
            existing["policy_list"] = row.get("policy_list")
            existing["policy_source"] = row.get("policy_source")
            existing["policy_source_url"] = row.get("policy_source_url")
            existing["policy_as_of"] = row.get("policy_as_of")

    tier_config = data_sources.get("model_jurisdiction_tiers") or {}
    tier_scoring = tier_config.get("scoring") or {}
    for tier_code in ("regulated_full", "basic_regulation", "offshore"):
        score = _as_float(tier_scoring.get(tier_code))
        for item in tier_config.get(tier_code) or []:
            if not isinstance(item, dict):
                continue
            row = {
                "name": item.get("name"),
                "iso2": item.get("iso2"),
                "iso3": item.get("iso3"),
                "list": tier_code,
                "score": score,
                "source": tier_config.get("source"),
                "as_of": tier_config.get("as_of"),
            }
            values = [item.get("name"), item.get("iso2"), item.get("iso3"), *(item.get("aliases") or [])]
            for value in values:
                put_row(value, dict(row))

    config = data_sources.get("fatf_jurisdictions") or {}
    scoring = config.get("scoring") or {}
    source_urls = config.get("source_urls") or {}
    for list_code in ("high_risk_call_for_action", "increased_monitoring", "vigilance"):
        score = _as_float(scoring.get(list_code))
        for item in config.get(list_code) or []:
            if not isinstance(item, dict):
                continue
            row = {
                "name": item.get("name"),
                "iso2": item.get("iso2"),
                "iso3": item.get("iso3"),
                "list": list_code,
                "score": score,
                "source": config.get("source"),
                "source_url": source_urls.get(list_code),
                "as_of": config.get("as_of"),
                "policy_list": list_code,
                "policy_source": config.get("source"),
                "policy_source_url": source_urls.get(list_code),
                "policy_as_of": config.get("as_of"),
            }
            values = [item.get("name"), item.get("iso2"), item.get("iso3"), *(item.get("aliases") or [])]
            for value in values:
                put_row(value, dict(row))

    return registry


def _jurisdiction_candidates(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, dict):
        candidates: list[Any] = []
        for key in ("jurisdiction", "country", "name", "iso2", "iso3", "code"):
            if value.get(key) not in (None, ""):
                candidates.append(value.get(key))
        return candidates
    if isinstance(value, (list, tuple, set)):
        candidates = []
        for item in value:
            candidates.extend(_jurisdiction_candidates(item))
        return candidates
    if isinstance(value, str):
        parts = [part.strip() for part in re.split(r"[/,;|]+", value) if part.strip()]
        return [value, *parts] if len(parts) > 1 else [value]
    return [value]


def lookup_jurisdiction(model: dict[str, Any], value: Any) -> dict[str, Any] | None:
    registry = build_jurisdiction_registry(model)
    best_match: dict[str, Any] | None = None

    for candidate in _jurisdiction_candidates(value):
        key = _normalize_key(candidate)
        if not key or key not in registry:
            continue
        match = dict(registry[key])
        match["input"] = candidate
        if best_match is None or _as_float(match.get("score")) > _as_float(best_match.get("score")):
            best_match = match

    return best_match


def canonical_tag(value: Any, aliases: dict[str, str]) -> str:
    key = _normalize_key(value)
    return aliases.get(key, key)


def score_zone(score: float, model: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    zones = model.get("zones") or {}
    for code, zone in zones.items():
        minimum = _as_float(zone.get("min"))
        maximum = _as_float(zone.get("max"), 100)
        if minimum <= score <= maximum:
            return str(code), zone
    if score >= 76 and "RED" in zones:
        return "RED", zones["RED"]
    if "GREEN" in zones:
        return "GREEN", zones["GREEN"]
    return "UNKNOWN", {"category": "unknown", "recommended_actions": []}


def _iter_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        if "tag" in value:
            return [str(value["tag"])]
        if "tags" in value:
            return _iter_tags(value["tags"])
        return []
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            result.extend(_iter_tags(item))
        return result
    return [str(value)]


def _tagged_address_rows(context: dict[str, Any], model: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def add_row(address: Any, value: Any, source: str) -> None:
        if not address:
            return
        tags = _iter_tags(value)
        note = ""
        custom_score = None
        if isinstance(value, dict):
            note = str(value.get("note") or value.get("reason") or "")
            custom_score = value.get("score")
        for tag in tags:
            rows.append(
                {
                    "address": str(address).strip(),
                    "tag": tag,
                    "source": source,
                    "note": note,
                    "custom_score": custom_score,
                }
            )

    tagged = context.get("tagged_addresses") or {}
    if isinstance(tagged, dict):
        for address, value in tagged.items():
            source = value.get("source") if isinstance(value, dict) else None
            add_row(address, value, str(source or "request.tagged_addresses"))
    elif isinstance(tagged, list):
        for item in tagged:
            if not isinstance(item, dict):
                continue
            add_row(item.get("address"), item, str(item.get("source") or "request.tagged_addresses"))

    for item in model.get("known_addresses") or []:
        if isinstance(item, dict):
            add_row(item.get("address"), item, "risk_model.known_addresses")

    return rows


def _add_exposure(
    exposures: list[dict[str, Any]],
    *,
    address: str,
    tag: Any,
    hop: int,
    source: str,
    canonical_tags: dict[str, dict[str, Any]],
    aliases: dict[str, str],
    note: str = "",
    custom_score: Any = None,
) -> None:
    code = canonical_tag(tag, aliases)
    meta = canonical_tags.get(code, {"code": code, "score": _as_float(custom_score), "label": str(tag), "category": "unknown"})
    score = _as_float(custom_score, _as_float(meta.get("score")))
    exposures.append(
        {
            "address": address,
            "tag": code,
            "tag_label": meta.get("label") or code,
            "tag_category": meta.get("category") or "unknown",
            "tag_score": score,
            "hop": int(max(0, hop)),
            "source": source,
            "note": note,
        }
    )


def collect_risk_exposures(
    *,
    wallet_report: dict[str, Any],
    trace_result: dict[str, Any] | None,
    context: dict[str, Any],
    model: dict[str, Any],
) -> list[dict[str, Any]]:
    canonical_tags, aliases = build_tag_registry(model)
    exposures: list[dict[str, Any]] = []
    root_address = str(wallet_report.get("address") or context.get("address") or "").strip()
    root_key = root_address.lower()

    for tag_value in _iter_tags(context.get("risk_tags")):
        _add_exposure(
            exposures,
            address=root_address,
            tag=tag_value,
            hop=0,
            source="request.risk_tags",
            canonical_tags=canonical_tags,
            aliases=aliases,
        )

    tagged_rows = _tagged_address_rows(context, model)
    tags_by_address: dict[str, list[dict[str, Any]]] = {}
    for row in tagged_rows:
        tags_by_address.setdefault(str(row["address"]).lower(), []).append(row)

    if root_key in tags_by_address:
        for row in tags_by_address[root_key]:
            _add_exposure(
                exposures,
                address=root_address,
                tag=row["tag"],
                hop=0,
                source=row["source"],
                canonical_tags=canonical_tags,
                aliases=aliases,
                note=row.get("note") or "",
                custom_score=row.get("custom_score"),
            )

    for counterparty in wallet_report.get("counterparties") or []:
        address = str(counterparty.get("address") or "").strip()
        if not address:
            continue
        for row in tags_by_address.get(address.lower(), []):
            _add_exposure(
                exposures,
                address=address,
                tag=row["tag"],
                hop=1,
                source=row["source"],
                canonical_tags=canonical_tags,
                aliases=aliases,
                note=row.get("note") or "",
                custom_score=row.get("custom_score"),
            )

    if trace_result:
        for node in trace_result.get("nodes") or []:
            address = str(node.get("address") or "").strip()
            if not address:
                continue
            raw_depth = _as_float(node.get("depth"), 0)
            hop = int(abs(raw_depth))
            for row in tags_by_address.get(address.lower(), []):
                _add_exposure(
                    exposures,
                    address=address,
                    tag=row["tag"],
                    hop=hop,
                    source=f"{row['source']}+trace",
                    canonical_tags=canonical_tags,
                    aliases=aliases,
                    note=row.get("note") or "",
                    custom_score=row.get("custom_score"),
                )

        for edge in trace_result.get("edges") or []:
            hop = int(max(1, _as_float(edge.get("trace_distance") or edge.get("depth"), 1)))
            for address_key in ("from_address", "to_address"):
                address = str(edge.get(address_key) or "").strip()
                if not address:
                    continue
                for row in tags_by_address.get(address.lower(), []):
                    _add_exposure(
                        exposures,
                        address=address,
                        tag=row["tag"],
                        hop=hop,
                        source=f"{row['source']}+trace_edge",
                        canonical_tags=canonical_tags,
                        aliases=aliases,
                        note=row.get("note") or "",
                        custom_score=row.get("custom_score"),
                    )

    deduped: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    for exposure in exposures:
        key = (
            str(exposure["address"]).lower(),
            exposure["tag"],
            int(exposure["hop"]),
            exposure["source"],
        )
        existing = deduped.get(key)
        if existing and _as_float(existing.get("tag_score")) >= _as_float(exposure.get("tag_score")):
            continue
        deduped[key] = exposure

    return sorted(
        deduped.values(),
        key=lambda row: (int(row.get("hop", 99)), -_as_float(row.get("tag_score")), str(row.get("tag"))),
    )


def extract_wallet_metrics(
    wallet_report: dict[str, Any],
    exposures: list[dict[str, Any]],
    *,
    context: dict[str, Any] | None = None,
    model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = wallet_report.get("summary") or []
    turnover = wallet_report.get("turnover") or {}
    frequency = wallet_report.get("frequency") or {}
    counterparties = wallet_report.get("counterparties") or []
    kyt_metrics = build_external_kyt_metrics(
        (context or {}).get("external_kyt"),
        model or {},
    )

    volume_total_usd = 0.0
    for row in summary:
        volume_total_usd += _as_float(row.get("volume_total_usdt") or row.get("volume_total_usd"))
    if volume_total_usd <= 0:
        for row in turnover.get("assets") or []:
            volume_total_usd += _as_float(row.get("volume_total_usd"))

    transaction_flags: dict[str, int] = {}
    for row in wallet_report.get("normalized_transactions") or []:
        flags = row.get("risk_flags") or []
        if isinstance(flags, str):
            flags = [part for part in flags.split("|") if part]
        for flag in flags:
            key = _normalize_key(flag)
            transaction_flags[key] = transaction_flags.get(key, 0) + 1

    max_tag_score = max((_as_float(row.get("tag_score")) for row in exposures), default=0.0)
    risk_hops = [int(row.get("hop", 99)) for row in exposures if _as_float(row.get("tag_score")) > 0]
    closest_risk_hop = min(risk_hops) if risk_hops else None

    tx_count = _as_float((wallet_report.get("counts") or {}).get("total"), len(wallet_report.get("transactions") or []))
    external_tx_count = _as_float(kyt_metrics.get("external_kyt_tx_count"))
    if external_tx_count > tx_count:
        tx_count = external_tx_count

    tx_per_active_day = _as_float(frequency.get("tx_per_active_day"))
    external_tx_per_active_day = _as_float(kyt_metrics.get("external_kyt_tx_per_active_day"))
    if external_tx_per_active_day > tx_per_active_day:
        tx_per_active_day = external_tx_per_active_day
    tx_per_month = round(tx_per_active_day * 30.4375, 2) if tx_per_active_day else 0.0

    return {
        "volume_total_usd": volume_total_usd,
        "tx_count": tx_count,
        "tx_per_active_day": tx_per_active_day,
        "tx_per_month": tx_per_month,
        "unique_counterparties": len(counterparties) or _as_float(turnover.get("unique_counterparties")),
        "rapid_transit_count": _as_float((turnover.get("rapid_transit") or {}).get("count")),
        "max_risk_tag_score": max_tag_score,
        "closest_risk_hop": closest_risk_hop,
        "transaction_flags": transaction_flags,
        "suspicious_patterns": wallet_report.get("suspicious_patterns") or [],
        **kyt_metrics,
    }


def _score_thresholds(value: Any, rule: dict[str, Any]) -> float:
    if value is None or value == "":
        return _as_float(rule.get("missing_score"))
    numeric = _as_float(value)
    for threshold in rule.get("thresholds") or []:
        if "gte" in threshold and numeric >= _as_float(threshold.get("gte")):
            return _as_float(threshold.get("score"))
        if "lte" in threshold and numeric <= _as_float(threshold.get("lte")):
            return _as_float(threshold.get("score"))
    return _as_float(rule.get("default_score"))


def _read_source_value(source: str, data: dict[str, Any], default: Any = None) -> Any:
    if source.startswith("context."):
        return _get_path(data["context"], source.removeprefix("context."), default)
    if source.startswith("metrics."):
        return _get_path(data["metrics"], source.removeprefix("metrics."), default)
    if source.startswith("model."):
        return _get_path(data["model"], source.removeprefix("model."), default)
    return default


def _resolve_rule_value(rule: dict[str, Any], data: dict[str, Any]) -> tuple[Any, str]:
    sources = [str(rule.get("source") or "")]
    sources.extend(str(source or "") for source in rule.get("fallback_sources") or [])

    for source in sources:
        if not source:
            continue
        value = _read_source_value(source, data, None)
        if value not in (None, ""):
            return value, source

    return rule.get("default"), next((source for source in sources if source), "")


def _score_component(rule: dict[str, Any], data: dict[str, Any]) -> tuple[float, Any, list[str]]:
    kind = rule.get("kind")
    evidence: list[str] = []

    if kind == "pattern_severity":
        severity_scores = rule.get("severity_scores") or {}
        patterns = data["metrics"].get("suspicious_patterns") or []
        score = 0.0
        for pattern in patterns:
            severity = _normalize_key(pattern.get("severity") if isinstance(pattern, dict) else "")
            score = max(score, _as_float(severity_scores.get(severity)))
            if isinstance(pattern, dict):
                evidence.append(pattern.get("code") or pattern.get("title") or severity)
        return score, [item for item in patterns[:10]], evidence

    if kind == "transaction_flags":
        flag_scores = rule.get("flag_scores") or {}
        flags = data["metrics"].get("transaction_flags") or {}
        score = 0.0
        for flag, count in flags.items():
            score += _as_float(flag_scores.get(_normalize_key(flag))) * _as_float(count)
        return _clamp(score), flags, [f"{flag}:{count}" for flag, count in sorted(flags.items())]

    value, source = _resolve_rule_value(rule, data)

    if kind == "jurisdiction_list":
        match = lookup_jurisdiction(data.get("model") or {}, value)
        if match:
            value_payload = {
                "input": match.get("input"),
                "source": source,
                "matched_name": match.get("name"),
                "list": match.get("list"),
                "policy_list": match.get("policy_list"),
                "iso2": match.get("iso2"),
                "iso3": match.get("iso3"),
                "as_of": match.get("as_of"),
            }
            evidence = [
                part
                for part in (
                    match.get("source"),
                    match.get("list"),
                    match.get("as_of"),
                )
                if part
            ]
            return _as_float(match.get("score")), value_payload, evidence

    if "options" in rule:
        option_key = _normalize_key(value if value not in (None, "") else rule.get("default"))
        options = rule.get("options") or {}
        if option_key in options or "thresholds" not in rule:
            score = _as_float(options.get(option_key), _as_float(rule.get("default_score")))
            return score, value, evidence

    if "thresholds" in rule:
        return _score_thresholds(value, rule), value, evidence

    return _as_float(rule.get("default_score")), value, evidence


def score_blocks(model: dict[str, Any], data: dict[str, Any]) -> tuple[dict[str, float], dict[str, Any]]:
    cap = _as_float((model.get("model") or {}).get("block_score_cap"), 100)
    block_scores: dict[str, float] = {}
    block_details: dict[str, Any] = {}

    for block_code, block in (model.get("blocks") or {}).items():
        components = block.get("components") or {}
        component_details: dict[str, Any] = {}
        component_scores: list[float] = []

        for component_code, rule in components.items():
            score, value, evidence = _score_component(rule or {}, data)
            score = _clamp(score, 0, cap)
            component_scores.append(score)
            component_details[component_code] = {
                "label": rule.get("label") or component_code,
                "score": round(score, 2),
                "value": value,
                "evidence": evidence,
            }

        block_score = sum(component_scores) / len(component_scores) if component_scores else 0.0
        block_scores[block_code] = round(_clamp(block_score, 0, cap), 2)
        block_details[block_code] = {
            "label": block.get("label") or block_code,
            "score": block_scores[block_code],
            "components": component_details,
        }

    return block_scores, block_details


def evaluate_policy_adjustments(model: dict[str, Any], block_details: dict[str, Any]) -> list[dict[str, Any]]:
    return evaluate_fatf_policy_adjustments(model, block_details)


def evaluate_fatf_policy_adjustments(model: dict[str, Any], block_details: dict[str, Any]) -> list[dict[str, Any]]:
    fatf_config = ((model.get("data_sources") or {}).get("fatf_jurisdictions") or {})
    policies = fatf_config.get("policy") or {}
    a1_value = (
        ((block_details.get("A") or {}).get("components") or {})
        .get("A1_jurisdiction", {})
        .get("value")
    )

    if not isinstance(a1_value, dict):
        return []

    list_code = a1_value.get("policy_list") or a1_value.get("list")
    policy = policies.get(list_code) or {}
    if not policy:
        return []

    return [
        {
            "id": f"fatf_{list_code}",
            "type": "fatf_jurisdiction_policy",
            "jurisdiction": a1_value,
            "minimum_score": _as_float(policy.get("minimum_score")),
            "recommended_actions": list(policy.get("recommended_actions") or []),
            "reason": policy.get("reason"),
        }
    ]


def evaluate_external_kyt_adjustments(context: dict[str, Any]) -> list[dict[str, Any]]:
    external_kyt = context.get("external_kyt")
    if not isinstance(external_kyt, dict) or not external_kyt:
        return []

    score_policy = str(external_kyt.get("score_policy") or "").strip().lower()
    if score_policy not in {"minimum_score", "score_floor"} and not external_kyt.get("use_provider_score_floor"):
        return []

    score_value = None
    for key in ("risk_score", "score", "risk_percent", "risk_percentage"):
        if external_kyt.get(key) not in (None, ""):
            score_value = external_kyt.get(key)
            break

    score = _as_float(score_value, -1)
    if score < 0:
        return []

    score = round(_clamp(score), 2)
    top_tags = (
        external_kyt.get("top_exposures")
        or external_kyt.get("exposures")
        or external_kyt.get("top_tags")
        or external_kyt.get("non_zero_tags")
        or external_kyt.get("tags")
        or []
    )
    recommended_actions: list[str] = []
    if score >= 76:
        recommended_actions = ["manual_review", "restrict_operations", "prepare_compliance_report"]
    elif score >= 51:
        recommended_actions = ["manual_review", "restrict_operations"]
    elif score >= 26:
        recommended_actions = ["enhanced_monitoring", "request_sof_sow"]

    return [
        {
            "id": "external_kyt_minimum_score",
            "type": "external_kyt_score_policy",
            "source": external_kyt.get("source") or "external_kyt",
            "minimum_score": score,
            "recommended_actions": recommended_actions,
            "reason": "External KYT provider risk score is explicitly used as a minimum score floor",
            "external_kyt": {
                "risk_score": score,
                "risk_zone": external_kyt.get("risk_zone"),
                "report_date": external_kyt.get("report_date"),
                "address": external_kyt.get("address"),
                "network": external_kyt.get("network"),
                "top_tags": top_tags[:10] if isinstance(top_tags, list) else [],
            },
        }
    ]


def evaluate_overrides(
    model: dict[str, Any],
    *,
    exposures: list[dict[str, Any]],
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    _, aliases = build_tag_registry(model)
    hits: list[dict[str, Any]] = []

    sanctions_screening = context.get("sanctions_screening")
    if isinstance(sanctions_screening, dict) and sanctions_screening.get("override_hit"):
        hits.append(
            {
                "id": "sanctions_screening_confirmed",
                "reason": "Confirmed match against sanctions screening data",
                "risk_zone": "RED",
                "final_score": 100,
                "sanctions_screening": {
                    "risk_tag": sanctions_screening.get("risk_tag", "sanction"),
                    "score": sanctions_screening.get("score", 100),
                    "matches": sanctions_screening.get("matches", [])[:10],
                },
            }
        )

    for rule in model.get("override_rules") or []:
        rule_type = rule.get("type")
        if rule_type == "tag_within_hops":
            tags = {canonical_tag(tag, aliases) for tag in rule.get("tags") or []}
            max_hop = rule.get("max_hop")
            for exposure in exposures:
                if exposure.get("tag") not in tags:
                    continue
                if max_hop is not None and int(exposure.get("hop", 99)) > int(max_hop):
                    continue
                hits.append(
                    {
                        "id": rule.get("id"),
                        "reason": rule.get("reason"),
                        "risk_zone": rule.get("risk_zone", "RED"),
                        "final_score": _as_float(rule.get("final_score"), 100),
                        "exposure": exposure,
                    }
                )
                break

        if rule_type == "context_equals":
            actual = _normalize_key(_get_path(context, str(rule.get("path") or ""), ""))
            values = rule.get("values")
            expected = {_normalize_key(value) for value in values} if isinstance(values, list) else {_normalize_key(rule.get("value"))}
            if actual and actual in expected:
                hits.append(
                    {
                        "id": rule.get("id"),
                        "reason": rule.get("reason"),
                        "risk_zone": rule.get("risk_zone", "RED"),
                        "final_score": _as_float(rule.get("final_score"), 100),
                        "context_path": rule.get("path"),
                        "context_value": actual,
                    }
                )

    return hits


def evaluate_data_quality(context: dict[str, Any], trace_result: dict[str, Any] | None) -> dict[str, Any]:
    participant_profile = context.get("participant_profile") if isinstance(context.get("participant_profile"), dict) else {}
    control_profile = context.get("control_profile") if isinstance(context.get("control_profile"), dict) else {}

    participant_fields = (
        "jurisdiction",
        "license_status",
        "ubo_transparency",
        "reputation",
        "sof_sow_status",
    )
    control_fields = (
        "aml_kyc_status",
        "client_funds_segregation",
        "regulatory_reporting",
        "request_response",
    )

    missing_participant = [field for field in participant_fields if participant_profile.get(field) in (None, "")]
    missing_control = [field for field in control_fields if control_profile.get(field) in (None, "")]
    total_fields = len(participant_fields) + len(control_fields)
    present_fields = total_fields - len(missing_participant) - len(missing_control)
    completeness = round((present_fields / total_fields) * 100, 2) if total_fields else 100.0

    if completeness >= 80:
        confidence = "high"
    elif completeness >= 40:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "assessment_confidence": confidence,
        "profile_completeness_pct": completeness,
        "missing_participant_profile_fields": missing_participant,
        "missing_control_profile_fields": missing_control,
        "missing_data_note": (
            "Risk score is based on incomplete participant/control profile data"
            if missing_participant or missing_control
            else ""
        ),
        "profile_data_supplied": bool(participant_profile or control_profile),
        "trace_used": bool(trace_result),
    }


def assess_wallet_risk(
    wallet_report: dict[str, Any],
    *,
    trace_result: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    model_path: str | Path = DEFAULT_MODEL_PATH,
) -> dict[str, Any]:
    model = load_risk_model(model_path)
    context = dict(context or {})
    context.setdefault("address", wallet_report.get("address"))
    context.setdefault("chain", wallet_report.get("network"))

    exposures = collect_risk_exposures(
        wallet_report=wallet_report,
        trace_result=trace_result,
        context=context,
        model=model,
    )
    metrics = extract_wallet_metrics(wallet_report, exposures, context=context, model=model)
    data = {"context": context, "metrics": metrics, "model": model}
    blocks, block_details = score_blocks(model, data)

    weights = model.get("weights") or {}
    raw_score = 0.0
    for block_code, block_score in blocks.items():
        raw_score += _as_float(weights.get(block_code), 0.0) * block_score
    raw_score = round(_clamp(raw_score), 2)
    raw_zone_code, raw_zone = score_zone(raw_score, model)

    policy_adjustments = [
        *evaluate_policy_adjustments(model, block_details),
        *evaluate_external_kyt_adjustments(context),
        *evaluate_external_kyt_policy_adjustments(model, metrics),
    ]
    adjusted_score = raw_score
    for adjustment in policy_adjustments:
        adjusted_score = max(adjusted_score, _as_float(adjustment.get("minimum_score")))

    override_hits = evaluate_overrides(model, exposures=exposures, context=context)
    final_score = adjusted_score
    final_zone_code = raw_zone_code
    if override_hits:
        strongest = max(override_hits, key=lambda item: _as_float(item.get("final_score")))
        final_score = max(adjusted_score, _as_float(strongest.get("final_score"), 100))
        final_zone_code = str(strongest.get("risk_zone") or "RED")

    final_score = round(_clamp(final_score), 2)
    final_zone_code, final_zone = score_zone(final_score, model) if not override_hits else (final_zone_code, (model.get("zones") or {}).get(final_zone_code, {}))
    recommended_actions = list(final_zone.get("recommended_actions") or [])
    for adjustment in policy_adjustments:
        for action in adjustment.get("recommended_actions") or []:
            if action not in recommended_actions:
                recommended_actions.append(action)
    data_quality = evaluate_data_quality(context, trace_result)

    return {
        "address": wallet_report.get("address") or context.get("address"),
        "chain": wallet_report.get("network") or context.get("chain"),
        "model_version": (model.get("model") or {}).get("version"),
        "raw_score": raw_score,
        "final_score": final_score,
        "risk_zone": final_zone_code,
        "category": final_zone.get("category") or "unknown",
        "zone_label": final_zone.get("label") or final_zone_code,
        "monitoring_frequency": final_zone.get("monitoring_frequency"),
        "override_hit": bool(override_hits),
        "override_reasons": [hit.get("id") for hit in override_hits],
        "override_details": override_hits,
        "policy_adjustments": policy_adjustments,
        "external_kyt": context.get("external_kyt") if isinstance(context.get("external_kyt"), dict) and context.get("external_kyt") else None,
        "blocks": blocks,
        "block_details": block_details,
        "metrics": {
            **metrics,
            "suspicious_patterns": metrics.get("suspicious_patterns", [])[:20],
        },
        "risk_exposures": exposures[:100],
        "recommended_actions": recommended_actions,
        "recommended_action_labels": [
            (model.get("action_labels") or {}).get(action, action)
            for action in recommended_actions
        ],
        "data_quality": {
            "external_risk_provider": "not_configured",
            "risk_tags_basis": "db.address_risk_tags + risk_model.known_addresses + request.risk_tags + request.tagged_addresses",
            "kyt_provider": metrics.get("external_kyt_provider") or "",
            "kyt_cache_status": context.get("external_kyt_cache_status") or ((context.get("external_kyt") or {}).get("cache_status") if isinstance(context.get("external_kyt"), dict) else ""),
            "kyt_provider_report_id": context.get("external_kyt_provider_report_id") or ((context.get("external_kyt") or {}).get("provider_report_id") if isinstance(context.get("external_kyt"), dict) else ""),
            "kyt_basis": "provider normalized exposures + risk_model.external_kyt category scores",
            "external_kyt_error": context.get("external_kyt_error") or "",
            "wallet_collection_skipped": bool(context.get("wallet_collection_skipped")),
            "jurisdiction_basis": "participant_profile.jurisdiction/country; wallet address jurisdiction is not inferred",
            "fatf_jurisdictions_as_of": (((model.get("data_sources") or {}).get("fatf_jurisdictions") or {}).get("as_of")),
            "external_kyt_supplied": bool(metrics.get("external_kyt_supplied")),
            **data_quality,
        },
    }
