from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from app.core.settings import STABLE_SYMBOLS

def decimal_or_zero(value: Any) -> Decimal:
    try:
        if value in (None, ""):
            return Decimal("0")
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def decimal_to_str(value: Decimal, places: int = 12) -> str:
    quant = Decimal(1).scaleb(-places)
    with localcontext() as context:
        context.prec = max(50, len(value.as_tuple().digits) + places + 4)
        normalized = value.quantize(quant).normalize()
    return format(normalized, "f")


def amount_from_base_units(raw_value: Any, decimals: int = 6) -> Decimal:
    divisor = Decimal(10) ** max(decimals, 0)
    return decimal_or_zero(raw_value) / divisor


def iso_from_ms(timestamp_ms: Any) -> str:
    timestamp = decimal_or_zero(timestamp_ms)
    if timestamp <= 0:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return datetime.fromtimestamp(float(timestamp) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def iso_from_seconds(timestamp_s: Any) -> str:
    timestamp = decimal_or_zero(timestamp_s)
    if timestamp <= 0:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def infer_direction(address: str, from_address: str, to_address: str, fallback: Any = None) -> str:
    address_l = address.lower()
    if to_address.lower() == address_l:
        return "incoming"
    if from_address.lower() == address_l:
        return "outgoing"
    return "incoming" if str(fallback) == "1" else "outgoing" if str(fallback) == "2" else "incoming"


def is_success(row: dict[str, Any]) -> bool:
    result = row.get("contract_ret") or row.get("contractRet") or row.get("finalResult") or "SUCCESS"
    status = row.get("status")
    revert = row.get("revert")
    return str(result).upper() == "SUCCESS" and str(status) not in {"1", "false"} and not bool(revert)


def make_tx_uid(kind: str, row_hash: str, *parts: Any) -> str:
    clean_parts = [str(part or "") for part in parts]
    return ":".join([kind, row_hash, *clean_parts])


def build_price_maps(wallet_payload: dict[str, Any]) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    by_token: dict[str, Decimal] = {}
    by_symbol: dict[str, Decimal] = {}
    for token in wallet_payload.get("data", []) or []:
        token_id = str(token.get("token_id") or token.get("tokenId") or "")
        symbol = str(token.get("token_abbr") or token.get("tokenAbbr") or "").upper()
        price = decimal_or_zero(token.get("token_price_in_usd"))
        if price <= 0:
            continue
        if token_id:
            by_token[token_id] = price
        if symbol:
            by_symbol[symbol] = price
    for stable in STABLE_SYMBOLS:
        by_symbol.setdefault(stable, Decimal("1"))
    return by_token, by_symbol


def price_for(token_id: str, symbol: str, prices_by_token: dict[str, Decimal], prices_by_symbol: dict[str, Decimal]) -> Decimal:
    symbol = symbol.upper()
    if symbol in STABLE_SYMBOLS:
        return Decimal("1")
    return prices_by_token.get(token_id) or prices_by_symbol.get(symbol) or Decimal("0")


def usd_value(amount: Decimal, price: Decimal, is_error: bool) -> str:
    if is_error or price <= 0:
        return ""
    return decimal_to_str(amount * price, 6)


def first_text(source: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return str(value)
    return ""
