from __future__ import annotations

import hashlib
import hmac
from typing import Any
from urllib.parse import quote

import httpx

from app.core.settings import SHARD_API_SECRET, SHARD_BASE_URL, SHARD_PUBLIC_APP_ID, SHARD_TIMEOUT_SECONDS
from app.domain.risk.kyt import as_float


class ShardKytError(RuntimeError):
    pass


def _chain(value: Any) -> str:
    key = str(value or "").strip().lower()
    aliases = {"tron": "trx", "trx": "trx", "ethereum": "eth", "eth": "eth", "bitcoin": "btc", "btc": "btc"}
    return aliases.get(key, key)


def _default_token(chain: str) -> str:
    return {"trx": "usdt", "eth": "eth", "btc": "btc"}.get(chain, chain)


def _currency_tag(*, network: str, token: str | None = None) -> str:
    chain = _chain(network)
    asset = str(token or "").strip().lower() or _default_token(chain)
    if chain not in {"trx", "eth", "btc"}:
        raise ShardKytError(f"Shard KYT does not support network: {network}")
    return f"{chain}-{asset}"


def _hmac_signature(secret: str, uri: str, body: bytes = b"") -> str:
    digest = hmac.new(secret.encode("utf-8"), uri.encode("utf-8") + body, hashlib.sha256)
    return digest.hexdigest()


async def fetch_shard_address_risk(
    *,
    network: str,
    address: str,
    token: str | None = None,
    public_app_id: str | None = None,
    api_secret: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    app_id = public_app_id or SHARD_PUBLIC_APP_ID
    secret = api_secret or SHARD_API_SECRET
    if not app_id or not secret:
        raise ShardKytError("SHARD_PUBLIC_APP_ID and SHARD_API_SECRET are not configured")

    currency_tag = _currency_tag(network=network, token=token)
    uri = f"/external/api/v2/address/{quote(str(address).strip(), safe='')}/risks/{quote(currency_tag, safe='')}"
    headers = {
        "Accept": "application/json",
        "X-Public-App-ID": app_id,
        "X-Hash": _hmac_signature(secret, uri),
    }
    timeout = httpx.Timeout(SHARD_TIMEOUT_SECONDS, connect=min(15.0, SHARD_TIMEOUT_SECONDS))
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.get(f"{(base_url or SHARD_BASE_URL).rstrip('/')}{uri}", headers=headers)
        except httpx.HTTPError as exc:
            raise ShardKytError(f"Shard KYT request failed: {exc}") from exc

    if response.status_code >= 400:
        detail = response.text[:300].replace("\n", " ")
        raise ShardKytError(f"Shard KYT HTTP {response.status_code}: {detail}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise ShardKytError(f"Shard KYT returned non-JSON response: {response.text[:200]}") from exc

    if not isinstance(payload, dict):
        raise ShardKytError("Shard KYT returned an unexpected response")
    return payload


def _risk_score(payload: dict[str, Any]) -> float:
    for key in ("overall_risk_score", "max_risk_score", "risk_score"):
        if payload.get(key) not in (None, ""):
            return as_float(payload.get(key))
    return 0.0


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return as_float(value)


def _add_risk_tags(
    exposures: list[dict[str, Any]],
    *,
    block_name: str,
    block: Any,
    direct: bool,
) -> None:
    if not isinstance(block, dict):
        return
    block_score = as_float(block.get("risk_score"))
    items = block.get("risk_tags") if isinstance(block.get("risk_tags"), list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        tag = str(item.get("tag") or item.get("name") or "").strip()
        if not tag:
            continue
        percent = _optional_float(item.get("percent"))
        if percent is None:
            percent = min(100.0, block_score) if direct and block_score > 0 else 0.0
        exposures.append(
            {
                "provider": "shard",
                "name": tag,
                "tag": tag,
                "percent": percent,
                "provider_risk_score": block_score,
                "source_block": block_name,
            }
        )


def _add_flags(
    exposures: list[dict[str, Any]],
    *,
    block_name: str,
    block: Any,
) -> None:
    if not isinstance(block, dict):
        return
    block_score = as_float(block.get("risk_score"))
    flags = block.get("flags") if isinstance(block.get("flags"), list) else []
    for item in flags:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason") or block_name).strip()
        if not reason:
            continue
        flag_score = as_float(item.get("risk_score"), block_score)
        exposures.append(
            {
                "provider": "shard",
                "name": reason,
                "tag": reason,
                "percent": min(100.0, flag_score) if flag_score > 0 else 0.0,
                "provider_risk_score": flag_score,
                "source_block": block_name,
                "direction": item.get("direction"),
                "amount": item.get("amount"),
                "token": item.get("token"),
            }
        )


def _add_blacklist_categories(exposures: list[dict[str, Any]], payload: dict[str, Any]) -> None:
    for category in payload.get("categories") or []:
        name = str(category or "").strip()
        if not name:
            continue
        exposures.append(
            {
                "provider": "shard",
                "name": name,
                "tag": name,
                "percent": 100.0,
                "provider_risk_score": 100.0,
                "source_block": "categories",
            }
        )


def normalize_shard_address_risk(
    payload: dict[str, Any],
    *,
    network: str,
    address: str,
    token: str | None = None,
) -> dict[str, Any]:
    chain = _chain(network)
    asset = str(token or "").strip().lower() or _default_token(chain)
    score = _risk_score(payload)
    exposures: list[dict[str, Any]] = []

    for block_name in ("report_risk", "cluster_risk", "reputation_risk"):
        _add_risk_tags(exposures, block_name=block_name, block=payload.get(block_name), direct=True)
    for block_name in ("coins_risk", "historical_risk"):
        _add_risk_tags(exposures, block_name=block_name, block=payload.get(block_name), direct=False)
    for block_name in ("fatf_flags", "internal_flags", "token_flags"):
        _add_flags(exposures, block_name=block_name, block=payload.get(block_name))
    _add_blacklist_categories(exposures, payload)

    exposures = sorted(exposures, key=lambda row: as_float(row.get("percent")), reverse=True)
    owners = [str(item) for item in payload.get("owners") or [] if str(item).strip()]
    clusters = [str(item) for item in payload.get("clusters") or [] if str(item).strip()]
    token_list = [str(item) for item in payload.get("token_list") or [] if str(item).strip()]

    return {
        "provider": "shard",
        "source": "Shard Risk API",
        "score_policy": "evidence_only",
        "address": address,
        "network": {"trx": "tron", "eth": "ethereum", "btc": "bitcoin"}.get(chain, chain),
        "token": asset,
        "currency_tag": f"{chain}-{asset}",
        "risk_score": score,
        "overall_risk_score": as_float(payload.get("overall_risk_score"), score),
        "provider_calculated_score": score,
        "max_risk_score": as_float(payload.get("max_risk_score"), score),
        "max_risk_token": payload.get("max_risk_token"),
        "balance": payload.get("balance"),
        "exposures": exposures,
        "top_exposures": exposures[:10],
        "transactions": {"total": 0, "sent": 0, "received": 0},
        "balances": {"total": payload.get("balance"), "tokens": [{"asset": asset.upper(), "balance": payload.get("balance")}]},
        "owners": owners,
        "owner": owners[0] if owners else "",
        "clusters": clusters,
        "cluster": {"id": clusters[0] if clusters else None, "owner": owners[0] if owners else ""},
        "token_list": token_list,
        "categories": payload.get("categories") or [],
        "calculation_uid": payload.get("calculation_uid"),
        "risk_breakdown": {
            key: payload.get(key)
            for key in (
                "report_risk",
                "cluster_risk",
                "reputation_risk",
                "coins_risk",
                "fatf_flags",
                "internal_flags",
                "token_flags",
            )
            if payload.get(key) is not None
        },
    }
