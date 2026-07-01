from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.core.settings import RANEX_API_KEY, RANEX_BASE_URL, RANEX_TIMEOUT_SECONDS
from app.domain.risk.kyt import as_float, as_int, unix_to_iso


class RanexKytError(RuntimeError):
    pass


def _network(value: Any) -> str:
    key = str(value or "").strip().lower()
    aliases = {"trx": "tron", "btc": "bitcoin", "eth": "ethereum"}
    return aliases.get(key, key)


def _decimal_amount(raw_value: Any, decimals: int) -> str:
    try:
        value = Decimal(str(raw_value or "0"))
        divisor = Decimal(10) ** max(0, int(decimals))
        normalized = value / divisor
    except (InvalidOperation, ValueError, TypeError):
        return "0"
    return format(normalized.normalize(), "f")


async def fetch_ranex_screening(
    *,
    network: str,
    address: str,
    api_key: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    key = api_key or RANEX_API_KEY
    if not key:
        raise RanexKytError("RANEX_API_KEY is not configured")

    chain = _network(network)
    if chain not in {"bitcoin", "ethereum", "tron"}:
        raise RanexKytError(f"Ranex KYT does not support network: {network}")

    url = f"{(base_url or RANEX_BASE_URL).rstrip('/')}/api/v1/crypto-screening/{chain}/{address}"
    timeout = httpx.Timeout(RANEX_TIMEOUT_SECONDS, connect=min(15.0, RANEX_TIMEOUT_SECONDS))
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.get(url, headers={"X-API-Key": key, "Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise RanexKytError(f"Ranex KYT request failed: {exc}") from exc

    if response.status_code >= 400:
        detail = response.text[:300].replace("\n", " ")
        raise RanexKytError(f"Ranex KYT HTTP {response.status_code}: {detail}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise RanexKytError(f"Ranex KYT returned non-JSON response: {response.text[:200]}") from exc

    if not isinstance(payload, dict):
        raise RanexKytError("Ranex KYT returned an unexpected response")
    return payload


def normalize_ranex_screening(payload: dict[str, Any]) -> dict[str, Any]:
    address_info = payload.get("address") if isinstance(payload.get("address"), dict) else {}
    risk_info = (payload.get("risk") or {}).get("riskInfo") if isinstance(payload.get("risk"), dict) else {}
    risk_info = risk_info if isinstance(risk_info, dict) else {}
    reported = risk_info.get("reported") if isinstance(risk_info.get("reported"), dict) else {}
    calculated = risk_info.get("calculated") if isinstance(risk_info.get("calculated"), dict) else {}
    raw_items = calculated.get("items") if isinstance(calculated.get("items"), list) else []

    exposures = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        percent = as_float(item.get("percent"))
        if percent <= 0:
            continue
        exposures.append(
            {
                "provider": "ranex",
                "name": item.get("name") or "",
                "number": item.get("number"),
                "percent": percent,
                "percent_raw": as_float(item.get("percentRaw"), percent),
                "provider_risk_score": as_float(item.get("risk")),
                "provider_risk_raw": as_float(item.get("riskRaw"), item.get("risk")),
                "amount": item.get("total"),
                "amount_human": item.get("totalHuman"),
                "color": item.get("color"),
                "i18n": item.get("i18n") or {},
            }
        )
    exposures.sort(key=lambda row: as_float(row.get("percent")), reverse=True)

    stats = (payload.get("addressTransactionStats") or {}).get("stats")
    stats = stats if isinstance(stats, dict) else {}
    balance = payload.get("balance") if isinstance(payload.get("balance"), dict) else {}
    activity = (payload.get("addressActivity") or {}).get("activityInfo")
    activity = activity if isinstance(activity, dict) else {}
    cluster = (payload.get("clusterForAddress") or {}).get("clusterInfo")
    cluster = cluster if isinstance(cluster, dict) else {}

    tokens = []
    for token in balance.get("tokens") or []:
        if not isinstance(token, dict):
            continue
        decimals = as_int(token.get("decimals"), 0)
        tokens.append(
            {
                "asset": token.get("symbol") or token.get("name") or "",
                "name": token.get("name") or "",
                "decimals": decimals,
                "balance_raw": token.get("balance"),
                "balance": _decimal_amount(token.get("balance"), decimals),
            }
        )

    first_timestamp = as_int(activity.get("first"), 0)
    last_timestamp = as_int(activity.get("last"), 0)

    return {
        "provider": "ranex",
        "source": "Ranex KYT API",
        "score_policy": "evidence_only",
        "address": address_info.get("addressValue") or "",
        "network": _network(address_info.get("network")),
        "owner": address_info.get("owner") or "",
        "risk_score": as_float(risk_info.get("risk")),
        "provider_calculated_score": as_float(calculated.get("risk")),
        "provider_reported": {
            "risk": as_float(reported.get("risk")),
            "name": reported.get("name"),
            "number": reported.get("number"),
        },
        "total": calculated.get("total"),
        "total_human": calculated.get("totalHuman"),
        "exposures": exposures,
        "top_exposures": exposures[:10],
        "transactions": {
            "total": as_int(stats.get("total"), 0),
            "sent": as_int(stats.get("sent"), 0),
            "received": as_int(stats.get("received"), 0),
        },
        "balances": {
            "sent": balance.get("sent"),
            "received": balance.get("received"),
            "total": balance.get("total"),
            "tokens": tokens,
        },
        "activity": {
            "first_timestamp": first_timestamp,
            "last_timestamp": last_timestamp,
            "first": unix_to_iso(first_timestamp),
            "last": unix_to_iso(last_timestamp),
        },
        "cluster": {
            "id": cluster.get("id"),
            "owner": cluster.get("owner") or "",
            "risk_category": cluster.get("riskCategory"),
            "risk_version": cluster.get("riskVersion"),
        },
        "errors": {
            "risk": (payload.get("risk") or {}).get("errors") if isinstance(payload.get("risk"), dict) else [],
            "transactions": (payload.get("addressTransactionStats") or {}).get("errors")
            if isinstance(payload.get("addressTransactionStats"), dict)
            else [],
            "activity": (payload.get("addressActivity") or {}).get("errors")
            if isinstance(payload.get("addressActivity"), dict)
            else [],
        },
    }
