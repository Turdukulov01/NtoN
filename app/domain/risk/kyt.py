from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any
import unicodedata


def normalize_key(value: Any) -> str:
    text = str(value or "").strip().lower().replace("ё", "е")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^0-9a-zа-я]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_")


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return default


def clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    return max(minimum, min(maximum, value))


def unix_to_iso(value: Any) -> str | None:
    timestamp = as_int(value, 0)
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def build_kyt_category_registry(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    config = model.get("external_kyt") or {}
    categories = config.get("category_scores") or {}
    registry: dict[str, dict[str, Any]] = {}

    for raw_name, raw_meta in categories.items():
        meta = dict(raw_meta or {})
        meta["name"] = raw_name
        for value in [raw_name, *(meta.get("aliases") or [])]:
            key = normalize_key(value)
            if key:
                registry[key] = meta

    return registry


def iter_external_kyt_exposures(external_kyt: Any) -> list[dict[str, Any]]:
    if not isinstance(external_kyt, dict):
        return []

    raw_items = (
        external_kyt.get("exposures")
        or external_kyt.get("items")
        or external_kyt.get("top_exposures")
        or external_kyt.get("non_zero_tags")
        or external_kyt.get("tags")
        or []
    )
    if not isinstance(raw_items, list):
        return []

    exposures: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("category") or item.get("tag") or "").strip()
        if not name:
            continue
        percent = clamp(as_float(item.get("percent", item.get("percentage"))))
        exposures.append(
            {
                **item,
                "name": name,
                "category_key": normalize_key(name),
                "percent": percent,
                "provider_risk_score": as_float(item.get("provider_risk_score", item.get("risk"))),
                "amount": item.get("amount") or item.get("total"),
                "amount_human": item.get("amount_human") or item.get("totalHuman"),
            }
        )

    return sorted(exposures, key=lambda row: row.get("percent", 0), reverse=True)


def _activity_days(external_kyt: dict[str, Any]) -> float:
    activity = external_kyt.get("activity") if isinstance(external_kyt.get("activity"), dict) else {}
    first = as_int(activity.get("first_timestamp") or activity.get("first"), 0)
    last = as_int(activity.get("last_timestamp") or activity.get("last"), 0)
    if first <= 0 or last <= first:
        return 0.0
    return max(1.0, (last - first) / 86400.0)


def _first_present(source: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return value
    return None


def build_external_kyt_metrics(external_kyt: Any, model: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(external_kyt, dict) or not external_kyt:
        return {
            "external_kyt_supplied": False,
            "external_kyt_weighted_exposure_score": 0.0,
            "external_kyt_provider_score": 0.0,
            "external_kyt_exposure_groups": {},
            "external_kyt_exposures": [],
            "external_kyt_tx_count": 0,
            "external_kyt_tx_per_active_day": 0.0,
            "external_kyt_sanctions_exposure_percent": 0.0,
            "external_kyt_illicit_exposure_percent": 0.0,
            "external_kyt_obfuscation_exposure_percent": 0.0,
            "external_kyt_high_risk_service_exposure_percent": 0.0,
            "external_kyt_mixer_exposure_percent": 0.0,
            "external_kyt_bridge_exposure_percent": 0.0,
            "external_kyt_mixer_bridge_exposure_percent": 0.0,
            "external_kyt_dex_exposure_percent": 0.0,
            "external_kyt_liquidity_pool_exposure_percent": 0.0,
        }

    registry = build_kyt_category_registry(model)
    group_percents: dict[str, float] = {}
    group_scores: dict[str, float] = {}
    category_percents: dict[str, float] = {}
    normalized_exposures: list[dict[str, Any]] = []
    weighted_score = 0.0
    max_severity = 0.0

    for exposure in iter_external_kyt_exposures(external_kyt):
        meta = registry.get(exposure["category_key"]) or {}
        severity = clamp(as_float(meta.get("score"), 0.0))
        group = str(meta.get("group") or "other")
        category = str(meta.get("name") or exposure["category_key"])
        percent = clamp(as_float(exposure.get("percent")))
        contribution = round((percent * severity) / 100.0, 4)
        weighted_score += contribution
        max_severity = max(max_severity, severity if percent > 0 else 0)
        category_percents[category] = round(category_percents.get(category, 0.0) + percent, 4)
        group_percents[group] = round(group_percents.get(group, 0.0) + percent, 4)
        group_scores[group] = round(group_scores.get(group, 0.0) + contribution, 4)
        normalized_exposures.append(
            {
                **exposure,
                "group": group,
                "model_category_score": severity,
                "model_contribution_score": contribution,
                "model_label": meta.get("label") or exposure.get("name"),
            }
        )

    transactions = external_kyt.get("transactions") if isinstance(external_kyt.get("transactions"), dict) else {}
    tx_count = as_int(transactions.get("total"), 0)
    active_days = _activity_days(external_kyt)
    tx_per_active_day = round(tx_count / active_days, 2) if tx_count and active_days else 0.0

    return {
        "external_kyt_supplied": True,
        "external_kyt_provider": external_kyt.get("provider") or external_kyt.get("source") or "",
        "external_kyt_provider_score": as_float(_first_present(external_kyt, "risk_score", "provider_risk_score", "score")),
        "external_kyt_weighted_exposure_score": round(clamp(weighted_score), 2),
        "external_kyt_max_category_severity": round(max_severity, 2),
        "external_kyt_exposure_groups": {
            group: {
                "percent": round(percent, 4),
                "score": round(group_scores.get(group, 0.0), 4),
            }
            for group, percent in sorted(group_percents.items())
        },
        "external_kyt_exposures": normalized_exposures[:50],
        "external_kyt_tx_count": tx_count,
        "external_kyt_tx_per_active_day": tx_per_active_day,
        "external_kyt_sanctions_exposure_percent": round(group_percents.get("sanctions", 0.0), 4),
        "external_kyt_illicit_exposure_percent": round(group_percents.get("illicit", 0.0), 4),
        "external_kyt_obfuscation_exposure_percent": round(group_percents.get("obfuscation", 0.0), 4),
        "external_kyt_high_risk_service_exposure_percent": round(group_percents.get("high_risk_service", 0.0), 4),
        "external_kyt_mixer_exposure_percent": round(category_percents.get("mixing_service", 0.0), 4),
        "external_kyt_bridge_exposure_percent": round(category_percents.get("bridge", 0.0), 4),
        "external_kyt_mixer_bridge_exposure_percent": round(
            category_percents.get("mixing_service", 0.0) + category_percents.get("bridge", 0.0),
            4,
        ),
        "external_kyt_dex_exposure_percent": round(category_percents.get("dex", 0.0), 4),
        "external_kyt_liquidity_pool_exposure_percent": round(
            sum(percent for category, percent in category_percents.items() if "pool" in category),
            4,
        ),
    }


def evaluate_external_kyt_policy_adjustments(
    model: dict[str, Any],
    metrics: dict[str, Any],
) -> list[dict[str, Any]]:
    if not metrics.get("external_kyt_supplied"):
        return []

    rules = ((model.get("external_kyt") or {}).get("policy") or {}).get("exposure_rules") or []
    hits: list[dict[str, Any]] = []
    for rule in rules:
        metric_name = str(rule.get("metric") or "")
        if not metric_name:
            continue
        actual = as_float(metrics.get(metric_name), 0.0)
        minimum = as_float(rule.get("min_percent", rule.get("min")), 0.0)
        if actual < minimum:
            continue
        hits.append(
            {
                "id": rule.get("id") or f"external_kyt_{metric_name}",
                "type": "external_kyt_exposure_policy",
                "metric": metric_name,
                "actual": actual,
                "threshold": minimum,
                "minimum_score": as_float(rule.get("minimum_score")),
                "recommended_actions": list(rule.get("recommended_actions") or []),
                "reason": rule.get("reason"),
            }
        )

    return hits
