from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.core.formatting import (
    amount_from_base_units,
    decimal_or_zero,
    decimal_to_str,
    first_text,
    infer_direction,
    is_success,
    iso_from_ms,
    iso_from_seconds,
    make_tx_uid,
    price_for,
    usd_value,
)
from app.core.settings import STABLE_SYMBOLS

def normalize_trx_transfer(
    row: dict[str, Any],
    address: str,
    prices_by_symbol: dict[str, Decimal],
    index: int,
) -> dict[str, Any]:
    details = row.get("_details") if isinstance(row.get("_details"), dict) else {}
    contract_data = details.get("contractData") if isinstance(details.get("contractData"), dict) else {}
    trigger_info = details.get("trigger_info") if isinstance(details.get("trigger_info"), dict) else {}
    from_address = (
        first_text(row, "from", "from_address", "transferFromAddress", "ownerAddress", "owner_address")
        or first_text(details, "ownerAddress", "from")
        or first_text(contract_data, "owner_address")
    )
    to_address = (
        first_text(row, "to", "to_address", "transferToAddress", "toAddress")
        or first_text(details, "toAddress", "to")
        or first_text(contract_data, "to_address", "receiver_address", "contract_address")
    )
    decimals = int(row.get("decimals") or 6)
    amount = amount_from_base_units(row.get("amount") or details.get("amount") or contract_data.get("amount"), decimals)
    row_hash = str(row.get("hash") or details.get("hash") or "")
    is_error = not is_success(row)
    price = prices_by_symbol.get("TRX", Decimal("0"))
    direction = infer_direction(address, from_address, to_address, row.get("direction"))
    timestamp_ms = row.get("block_timestamp") or details.get("timestamp")
    contract_address = first_text(row, "contract_address") or first_text(contract_data, "contract_address")
    method = trigger_info.get("method") or row.get("contract_type") or details.get("contract_type") or "TRX transfer"

    return {
        "uid": make_tx_uid("trx", row_hash, from_address, to_address, decimal_to_str(amount), timestamp_ms),
        "wallet_id": "",
        "tx_hash": row_hash,
        "original_hash": row_hash,
        "timestamp": iso_from_ms(timestamp_ms),
        "block_number": row.get("block") or details.get("block") or "",
        "result": str(row.get("contract_ret") or row.get("contractRet") or details.get("contractRet") or "SUCCESS"),
        "status": "CONFIRMED" if row.get("confirmed") or details.get("confirmed") else "PENDING",
        "confirmations": "",
        "resources_fee": "",
        "from_address": from_address,
        "to_address": to_address,
        "amount": decimal_to_str(amount),
        "asset": "TRX",
        "direction": direction,
        "native_amount": decimal_to_str(amount),
        "native_asset": "TRX",
        "usd_value": usd_value(amount, price, is_error),
        "network": "tron",
        "is_error": is_error,
        "owner_address": from_address,
        "contract_address": contract_address,
        "token_from": from_address,
        "token_to": to_address,
        "token_amount": decimal_to_str(amount),
        "token_symbol": "TRX",
        "tx_type": "transfer",
        "resource_type": "",
        "resource_value": "",
        "staked_asset_released": "",
        "signature_addresses": [],
        "method": method,
        "method_id": "",
        "input_data": trigger_info.get("data") or contract_data.get("data") or "",
        "source": "tronscan",
    }


def normalize_trc20_transfer(
    row: dict[str, Any],
    address: str,
    prices_by_token: dict[str, Decimal],
    prices_by_symbol: dict[str, Decimal],
    index: int,
) -> dict[str, Any]:
    token_info = row.get("tokenInfo") or {}
    contract_address = str(row.get("contract_address") or token_info.get("tokenId") or "")
    symbol = str(token_info.get("tokenAbbr") or row.get("symbol") or "TRC20").upper()
    decimals = int(token_info.get("tokenDecimal") or row.get("decimals") or 6)
    amount = amount_from_base_units(row.get("quant") or row.get("amount"), decimals)
    from_address = first_text(row, "from_address", "from", "transferFromAddress", "ownerAddress", "owner_address")
    to_address = first_text(row, "to_address", "to", "transferToAddress", "toAddress")
    row_hash = str(row.get("transaction_id") or row.get("hash") or "")
    is_error = not is_success(row)
    price = price_for(contract_address, symbol, prices_by_token, prices_by_symbol)
    trigger = row.get("trigger_info") or {}

    return {
        "uid": make_tx_uid("trc20", row_hash, contract_address, from_address, to_address, row.get("quant") or row.get("amount")),
        "wallet_id": "",
        "tx_hash": row_hash,
        "original_hash": row_hash,
        "timestamp": iso_from_ms(row.get("block_ts") or row.get("block_timestamp")),
        "block_number": row.get("block") or "",
        "result": str(row.get("contractRet") or row.get("finalResult") or "SUCCESS"),
        "status": "CONFIRMED" if row.get("confirmed") else "PENDING",
        "confirmations": "",
        "resources_fee": "",
        "from_address": from_address,
        "to_address": to_address,
        "amount": decimal_to_str(amount),
        "asset": symbol,
        "direction": infer_direction(address, from_address, to_address),
        "native_amount": "",
        "native_asset": "TRX",
        "usd_value": usd_value(amount, price, is_error),
        "network": "tron",
        "is_error": is_error,
        "owner_address": from_address,
        "contract_address": contract_address,
        "token_from": from_address,
        "token_to": to_address,
        "token_amount": decimal_to_str(amount),
        "token_symbol": symbol,
        "tx_type": "transfer",
        "resource_type": "",
        "resource_value": "",
        "staked_asset_released": "",
        "signature_addresses": [],
        "method": trigger.get("method") or row.get("event_type") or "transfer()",
        "method_id": "",
        "input_data": trigger.get("data") or "",
        "source": "tronscan",
    }


def build_tron_balances(wallet_payload: dict[str, Any]) -> list[dict[str, Any]]:
    balances: list[dict[str, Any]] = []
    for token in wallet_payload.get("data", []) or []:
        if not isinstance(token, dict):
            continue
        symbol = str(
            token.get("token_abbr")
            or token.get("tokenAbbr")
            or token.get("tokenName")
            or token.get("token_name")
            or "UNKNOWN"
        ).upper()
        contract_address = str(token.get("token_id") or token.get("tokenId") or token.get("contract_address") or "")
        decimals = int(token.get("tokenDecimal") or token.get("decimals") or 6)
        if token.get("quantity") not in (None, ""):
            amount = decimal_or_zero(token.get("quantity"))
        else:
            raw_amount = decimal_or_zero(token.get("balance") or token.get("amount"))
            amount = raw_amount
            if raw_amount >= Decimal(10) ** max(decimals, 0):
                amount = amount_from_base_units(raw_amount, decimals)
        if amount <= 0:
            continue
        price = decimal_or_zero(token.get("token_price_in_usd") or token.get("priceInUsd"))
        balances.append(
            {
                "asset": "TRX" if symbol in {"_", "TRON"} else symbol,
                "amount": decimal_to_str(amount),
                "usd_value": decimal_to_str(amount * price, 6) if price > 0 else "",
                "contract_address": contract_address,
                "is_native": symbol in {"TRX", "_", "TRON"} or contract_address == "_",
                "estimated": False,
            }
        )
    return balances


def normalize_ethereum_native_transfer(row: dict[str, Any], address: str, index: int) -> dict[str, Any]:
    from_address = str(row.get("from") or "")
    to_address = str(row.get("to") or "")
    row_hash = str(row.get("hash") or "")
    amount = amount_from_base_units(row.get("value"), 18)
    gas_used = decimal_or_zero(row.get("gasUsed"))
    gas_price = decimal_or_zero(row.get("gasPrice"))
    fee = amount_from_base_units(gas_used * gas_price, 18) if gas_used and gas_price else Decimal("0")
    is_error = str(row.get("isError") or "0") == "1" or str(row.get("txreceipt_status") or "1") == "0"
    return {
        "uid": make_tx_uid("eth", row_hash, from_address, to_address, decimal_to_str(amount, 18), row.get("timeStamp")),
        "wallet_id": "",
        "tx_hash": row_hash,
        "original_hash": row_hash,
        "timestamp": iso_from_seconds(row.get("timeStamp")),
        "block_number": row.get("blockNumber") or "",
        "result": "ERROR" if is_error else "SUCCESS",
        "status": "CONFIRMED",
        "confirmations": row.get("confirmations") or "",
        "resources_fee": decimal_to_str(fee, 18) if fee > 0 else "",
        "from_address": from_address,
        "to_address": to_address,
        "amount": decimal_to_str(amount, 18),
        "asset": "ETH",
        "direction": infer_direction(address, from_address, to_address),
        "native_amount": decimal_to_str(amount, 18),
        "native_asset": "ETH",
        "usd_value": "",
        "network": "ethereum",
        "is_error": is_error,
        "owner_address": from_address,
        "contract_address": str(row.get("contractAddress") or ""),
        "token_from": from_address,
        "token_to": to_address,
        "token_amount": decimal_to_str(amount, 18),
        "token_symbol": "ETH",
        "tx_type": "transfer",
        "resource_type": "gas",
        "resource_value": decimal_to_str(fee, 18) if fee > 0 else "",
        "staked_asset_released": "",
        "signature_addresses": [],
        "method": row.get("functionName") or row.get("methodId") or "ETH transfer",
        "method_id": row.get("methodId") or "",
        "input_data": row.get("input") or "",
        "source": "etherscan",
    }


def normalize_ethereum_token_transfer(row: dict[str, Any], address: str, index: int) -> dict[str, Any]:
    from_address = str(row.get("from") or "")
    to_address = str(row.get("to") or "")
    row_hash = str(row.get("hash") or "")
    symbol = str(row.get("tokenSymbol") or "ERC20").upper()
    decimals = int(row.get("tokenDecimal") or 18)
    amount = amount_from_base_units(row.get("value"), decimals)
    is_error = str(row.get("isError") or "0") == "1"
    usd = decimal_to_str(amount, 6) if symbol in STABLE_SYMBOLS and not is_error else ""
    return {
        "uid": make_tx_uid("erc20", row_hash, row.get("contractAddress"), from_address, to_address, row.get("value")),
        "wallet_id": "",
        "tx_hash": row_hash,
        "original_hash": row_hash,
        "timestamp": iso_from_seconds(row.get("timeStamp")),
        "block_number": row.get("blockNumber") or "",
        "result": "ERROR" if is_error else "SUCCESS",
        "status": "CONFIRMED",
        "confirmations": row.get("confirmations") or "",
        "resources_fee": "",
        "from_address": from_address,
        "to_address": to_address,
        "amount": decimal_to_str(amount, decimals),
        "asset": symbol,
        "direction": infer_direction(address, from_address, to_address),
        "native_amount": "",
        "native_asset": "ETH",
        "usd_value": usd,
        "network": "ethereum",
        "is_error": is_error,
        "owner_address": from_address,
        "contract_address": str(row.get("contractAddress") or ""),
        "token_from": from_address,
        "token_to": to_address,
        "token_amount": decimal_to_str(amount, decimals),
        "token_symbol": symbol,
        "tx_type": "transfer",
        "resource_type": "",
        "resource_value": "",
        "staked_asset_released": "",
        "signature_addresses": [],
        "method": row.get("functionName") or "transfer()",
        "method_id": row.get("methodId") or "",
        "input_data": row.get("input") or "",
        "source": "etherscan",
    }


def normalize_bitcoin_transaction(row: dict[str, Any], address: str, index: int) -> dict[str, Any] | None:
    vin = row.get("vin") if isinstance(row.get("vin"), list) else []
    vout = row.get("vout") if isinstance(row.get("vout"), list) else []
    address_l = address.lower()

    outgoing_sats = Decimal("0")
    incoming_sats = Decimal("0")
    external_inputs: list[str] = []
    external_outputs: list[str] = []
    for item in vin:
        prevout = item.get("prevout") if isinstance(item.get("prevout"), dict) else {}
        prev_address = str(prevout.get("scriptpubkey_address") or "")
        value = decimal_or_zero(prevout.get("value"))
        if prev_address.lower() == address_l:
            outgoing_sats += value
        elif prev_address:
            external_inputs.append(prev_address)
    for item in vout:
        out_address = str(item.get("scriptpubkey_address") or "")
        value = decimal_or_zero(item.get("value"))
        if out_address.lower() == address_l:
            incoming_sats += value
        elif out_address:
            external_outputs.append(out_address)

    net_sats = incoming_sats - outgoing_sats
    if net_sats == 0:
        return None

    direction = "incoming" if net_sats > 0 else "outgoing"
    amount = abs(net_sats) / Decimal("100000000")
    from_address = external_inputs[0] if external_inputs else ("coinbase" if row.get("vin", [{}])[0].get("is_coinbase") else address)
    to_address = address if direction == "incoming" else (external_outputs[0] if external_outputs else address)
    if direction == "outgoing":
        from_address = address
    fee = decimal_or_zero(row.get("fee")) / Decimal("100000000")
    status = row.get("status") if isinstance(row.get("status"), dict) else {}
    timestamp = status.get("block_time")
    return {
        "uid": make_tx_uid("btc", row.get("txid") or "", direction, decimal_to_str(amount, 8), timestamp or ""),
        "wallet_id": "",
        "tx_hash": row.get("txid") or "",
        "original_hash": row.get("txid") or "",
        "timestamp": iso_from_seconds(timestamp) if timestamp else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "block_number": status.get("block_height") or "",
        "result": "SUCCESS",
        "status": "CONFIRMED" if status.get("confirmed") else "PENDING",
        "confirmations": "",
        "resources_fee": decimal_to_str(fee, 8) if fee > 0 else "",
        "from_address": from_address,
        "to_address": to_address,
        "amount": decimal_to_str(amount, 8),
        "asset": "BTC",
        "direction": direction,
        "native_amount": decimal_to_str(amount, 8),
        "native_asset": "BTC",
        "usd_value": "",
        "network": "bitcoin",
        "is_error": False,
        "owner_address": from_address,
        "contract_address": "",
        "token_from": from_address,
        "token_to": to_address,
        "token_amount": decimal_to_str(amount, 8),
        "token_symbol": "BTC",
        "tx_type": "transfer",
        "resource_type": "fee",
        "resource_value": decimal_to_str(fee, 8) if fee > 0 else "",
        "staked_asset_released": "",
        "signature_addresses": [],
        "method": "bitcoin transfer",
        "method_id": "",
        "input_data": "",
        "source": "blockstream",
    }

