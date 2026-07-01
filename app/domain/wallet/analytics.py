from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.core.formatting import decimal_or_zero, decimal_to_str, infer_direction, make_tx_uid
from app.core.periods import tx_datetime
from app.core.settings import STABLE_SYMBOLS

def build_balances_from_summary(summary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    balances: list[dict[str, Any]] = []
    for row in summary:
        amount = decimal_or_zero(row.get("net_flow"))
        if amount == 0:
            continue
        balances.append(
            {
                "asset": row.get("asset") or "UNKNOWN",
                "amount": decimal_to_str(amount),
                "usd_value": row.get("volume_total_usdt") or "",
                "estimated": True,
                "note": "net flow from loaded transactions",
            }
        )
    return balances


def build_wallet_activity(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    timestamps = [timestamp for timestamp in (tx_datetime(tx) for tx in transactions) if timestamp]
    if not timestamps:
        return {"first_seen": None, "last_seen": None, "active_days": 0}
    first_seen = min(timestamps)
    last_seen = max(timestamps)
    active_days = len({timestamp.date().isoformat() for timestamp in timestamps})
    return {
        "first_seen": first_seen.isoformat().replace("+00:00", "Z"),
        "last_seen": last_seen.isoformat().replace("+00:00", "Z"),
        "active_days": active_days,
    }


def build_wallet_counts(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    incoming = sum(1 for tx in transactions if tx.get("direction") == "incoming")
    outgoing = sum(1 for tx in transactions if tx.get("direction") == "outgoing")
    failed = sum(1 for tx in transactions if tx.get("is_error"))
    assets = sorted({str(tx.get("asset") or "UNKNOWN").upper() for tx in transactions})
    return {
        "incoming": incoming,
        "outgoing": outgoing,
        "failed": failed,
        "total": len(transactions),
        "assets": assets,
    }


def build_counterparties(address: str, transactions: list[dict[str, Any]], limit: int = 100) -> list[dict[str, Any]]:
    address_l = address.lower()
    groups: dict[str, dict[str, Any]] = {}
    for tx in transactions:
        if tx.get("is_error"):
            continue
        direction = str(tx.get("direction") or "")
        from_address = str(tx.get("from_address") or "")
        to_address = str(tx.get("to_address") or "")
        if direction == "incoming":
            counterparty = from_address
        elif direction == "outgoing":
            counterparty = to_address
        elif to_address.lower() == address_l:
            direction = "incoming"
            counterparty = from_address
        elif from_address.lower() == address_l:
            direction = "outgoing"
            counterparty = to_address
        else:
            continue
        if not counterparty:
            counterparty = "unknown"

        key = counterparty.lower()
        amount = decimal_or_zero(tx.get("amount"))
        usd = decimal_or_zero(tx.get("usd_value"))
        asset = str(tx.get("asset") or "UNKNOWN").upper()
        row = groups.setdefault(
            key,
            {
                "address": counterparty,
                "incoming_count": 0,
                "outgoing_count": 0,
                "received": {},
                "sent": {},
                "usd_value": Decimal("0"),
                "tx_count": 0,
                "first_seen": "",
                "last_seen": "",
            },
        )
        bucket_name = "received" if direction == "incoming" else "sent"
        row[bucket_name][asset] = row[bucket_name].get(asset, Decimal("0")) + amount
        row["usd_value"] += usd
        row["tx_count"] += 1
        if direction == "incoming":
            row["incoming_count"] += 1
        else:
            row["outgoing_count"] += 1
        timestamp = str(tx.get("timestamp") or "")
        if timestamp:
            if not row["first_seen"] or timestamp < row["first_seen"]:
                row["first_seen"] = timestamp
            if not row["last_seen"] or timestamp > row["last_seen"]:
                row["last_seen"] = timestamp

    result: list[dict[str, Any]] = []
    for row in groups.values():
        result.append(
            {
                **row,
                "received": {asset: decimal_to_str(amount) for asset, amount in sorted(row["received"].items())},
                "sent": {asset: decimal_to_str(amount) for asset, amount in sorted(row["sent"].items())},
                "usd_value": decimal_to_str(row["usd_value"], 6),
            }
        )
    return sorted(result, key=lambda item: (decimal_or_zero(item["usd_value"]), item["tx_count"]), reverse=True)[:limit]


def build_operation_frequency(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    by_day: dict[str, int] = {}
    by_hour: dict[str, int] = {}
    timestamps = sorted(timestamp for timestamp in (tx_datetime(tx) for tx in transactions) if timestamp)
    for timestamp in timestamps:
        day = timestamp.date().isoformat()
        hour = str(timestamp.hour).zfill(2)
        by_day[day] = by_day.get(day, 0) + 1
        by_hour[hour] = by_hour.get(hour, 0) + 1
    active_days = len(by_day)
    busiest_day = max(by_day.items(), key=lambda item: item[1], default=("", 0))
    intervals = [
        (timestamps[index] - timestamps[index - 1]).total_seconds() / 60
        for index in range(1, len(timestamps))
    ]
    avg_interval = sum(intervals) / len(intervals) if intervals else None
    return {
        "active_days": active_days,
        "tx_per_active_day": round(len(timestamps) / active_days, 2) if active_days else 0,
        "busiest_day": busiest_day[0] or None,
        "busiest_day_count": busiest_day[1],
        "avg_minutes_between_transactions": round(avg_interval, 2) if avg_interval is not None else None,
        "by_day": by_day,
        "by_hour": by_hour,
    }


def is_dust_transfer(tx: dict[str, Any]) -> bool:
    amount = abs(decimal_or_zero(tx.get("amount")))
    asset = str(tx.get("asset") or "").upper()
    if amount <= 0:
        return True
    if asset == "BTC":
        return amount < Decimal("0.00001")
    if asset in {"ETH", "TRX"}:
        return amount < Decimal("0.001")
    if asset in STABLE_SYMBOLS:
        return amount < Decimal("1")
    return amount < Decimal("0.000001")


def detect_suspicious_patterns(
    transactions: list[dict[str, Any]],
    counterparties: list[dict[str, Any]],
    frequency: dict[str, Any],
) -> list[dict[str, Any]]:
    patterns: list[dict[str, Any]] = []
    if frequency.get("tx_per_active_day", 0) >= 50 or frequency.get("busiest_day_count", 0) >= 100:
        patterns.append(
            {
                "code": "high_frequency",
                "severity": "medium",
                "title": "Высокая частота операций",
                "description": "Много транзакций в коротком периоде, похоже на автоматизированный поток.",
            }
        )
    if sum(1 for item in counterparties if item.get("outgoing_count", 0) > 0) >= 25:
        patterns.append(
            {
                "code": "many_outgoing_counterparties",
                "severity": "medium",
                "title": "Много исходящих контрагентов",
                "description": "Адрес отправлял средства большому числу разных адресов.",
            }
        )
    if sum(1 for item in counterparties if item.get("incoming_count", 0) > 0) >= 25:
        patterns.append(
            {
                "code": "many_incoming_counterparties",
                "severity": "low",
                "title": "Много входящих контрагентов",
                "description": "Адрес получал средства от большого числа разных адресов.",
            }
        )
    if sum(1 for tx in transactions if is_dust_transfer(tx)) >= 20:
        patterns.append(
            {
                "code": "dust_activity",
                "severity": "low",
                "title": "Много мелких переводов",
                "description": "В истории много нулевых или очень маленьких переводов.",
            }
        )
    if sum(1 for tx in transactions if tx.get("is_error")) >= 5:
        patterns.append(
            {
                "code": "failed_operations",
                "severity": "low",
                "title": "Повторяющиеся ошибки",
                "description": "Есть несколько неуспешных операций.",
            }
        )

    ordered = sorted((tx for tx in transactions if not tx.get("is_error")), key=lambda item: tx_datetime(item) or datetime.min.replace(tzinfo=timezone.utc))
    last_incoming: datetime | None = None
    for tx in ordered:
        timestamp = tx_datetime(tx)
        if not timestamp:
            continue
        if tx.get("direction") == "incoming":
            last_incoming = timestamp
        elif tx.get("direction") == "outgoing" and last_incoming:
            if (timestamp - last_incoming).total_seconds() <= 600:
                patterns.append(
                    {
                        "code": "rapid_in_out",
                        "severity": "medium",
                        "title": "Быстрый вход-выход",
                        "description": "После входящего перевода вскоре был исходящий перевод.",
                    }
                )
                break
    return patterns


def transaction_risk_flags(tx: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    amount = decimal_or_zero(tx.get("amount"))
    usd = decimal_or_zero(tx.get("usd_value"))
    if tx.get("is_error"):
        flags.append("failed")
    if amount <= 0:
        flags.append("zero_amount")
    if is_dust_transfer(tx):
        flags.append("dust")
    if not tx.get("from_address") or not tx.get("to_address"):
        flags.append("missing_counterparty")
    if usd >= Decimal("100000"):
        flags.append("large_usd_transfer")
    return flags


def normalize_transaction_record(
    tx: dict[str, Any],
    *,
    chain: str,
    wallet_address: str,
) -> dict[str, Any]:
    direction = str(tx.get("direction") or "")
    from_address = str(tx.get("from_address") or tx.get("token_from") or "").strip()
    to_address = str(tx.get("to_address") or tx.get("token_to") or "").strip()
    if direction == "incoming":
        counterparty = from_address
    elif direction == "outgoing":
        counterparty = to_address
    else:
        counterparty = to_address if from_address.lower() == wallet_address.lower() else from_address

    return {
        "id": tx.get("uid") or tx.get("tx_hash") or make_tx_uid("normalized", tx.get("tx_hash") or "", tx.get("asset")),
        "chain": chain,
        "tx_hash": tx.get("tx_hash") or tx.get("original_hash") or "",
        "wallet_address": wallet_address,
        "direction": direction or infer_direction(wallet_address, from_address, to_address),
        "counterparty_address": counterparty,
        "token_symbol": str(tx.get("asset") or tx.get("token_symbol") or "UNKNOWN").upper(),
        "amount": decimal_to_str(decimal_or_zero(tx.get("amount"))),
        "amount_usd": decimal_to_str(decimal_or_zero(tx.get("usd_value")), 6) if tx.get("usd_value") not in (None, "") else "",
        "timestamp": tx.get("timestamp") or "",
        "block_number": tx.get("block_number") or "",
        "risk_flags": transaction_risk_flags(tx),
        "raw_json": tx,
    }


def add_turnover_row(
    groups: dict[tuple[str, str], dict[str, Any]],
    key: tuple[str, str],
    tx: dict[str, Any],
    period_label: str,
) -> None:
    asset = str(tx.get("asset") or "UNKNOWN").upper()
    row = groups.setdefault(
        key,
        {
            "period": key[0],
            "period_label": period_label,
            "asset": asset,
            "volume_in": Decimal("0"),
            "volume_out": Decimal("0"),
            "volume_in_usd": Decimal("0"),
            "volume_out_usd": Decimal("0"),
            "tx_count_in": 0,
            "tx_count_out": 0,
        },
    )
    amount = decimal_or_zero(tx.get("amount"))
    usd = decimal_or_zero(tx.get("usd_value"))
    if tx.get("direction") == "incoming":
        row["volume_in"] += amount
        row["volume_in_usd"] += usd
        row["tx_count_in"] += 1
    else:
        row["volume_out"] += amount
        row["volume_out_usd"] += usd
        row["tx_count_out"] += 1


def serialize_turnover_row(row: dict[str, Any]) -> dict[str, Any]:
    volume_in = row["volume_in"]
    volume_out = row["volume_out"]
    volume_in_usd = row["volume_in_usd"]
    volume_out_usd = row["volume_out_usd"]
    return {
        "period": row["period"],
        "period_label": row["period_label"],
        "asset": row["asset"],
        "volume_in": decimal_to_str(volume_in),
        "volume_out": decimal_to_str(volume_out),
        "volume_total": decimal_to_str(volume_in + volume_out),
        "net_flow": decimal_to_str(volume_in - volume_out),
        "volume_in_usd": decimal_to_str(volume_in_usd, 6),
        "volume_out_usd": decimal_to_str(volume_out_usd, 6),
        "volume_total_usd": decimal_to_str(volume_in_usd + volume_out_usd, 6),
        "net_flow_usd": decimal_to_str(volume_in_usd - volume_out_usd, 6),
        "tx_count_in": row["tx_count_in"],
        "tx_count_out": row["tx_count_out"],
        "tx_count_total": row["tx_count_in"] + row["tx_count_out"],
    }


def build_rapid_transit(transactions: list[dict[str, Any]], window_minutes: int = 60) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    by_asset: dict[str, list[dict[str, Any]]] = {}
    for tx in transactions:
        if tx.get("is_error") or decimal_or_zero(tx.get("amount")) <= 0:
            continue
        asset = str(tx.get("asset") or "UNKNOWN").upper()
        by_asset.setdefault(asset, []).append(tx)

    for asset, rows in by_asset.items():
        sorted_rows = sorted(rows, key=lambda tx: tx_datetime(tx) or datetime.max.replace(tzinfo=timezone.utc))
        incoming_rows = [tx for tx in sorted_rows if tx.get("direction") == "incoming"]
        outgoing_rows = [tx for tx in sorted_rows if tx.get("direction") == "outgoing"]
        for incoming in incoming_rows:
            incoming_time = tx_datetime(incoming)
            incoming_amount = decimal_or_zero(incoming.get("amount"))
            if not incoming_time or incoming_amount <= 0:
                continue
            for outgoing in outgoing_rows:
                outgoing_time = tx_datetime(outgoing)
                if not outgoing_time or outgoing_time < incoming_time:
                    continue
                delta_minutes = (outgoing_time - incoming_time).total_seconds() / 60
                if delta_minutes > window_minutes:
                    break
                outgoing_amount = decimal_or_zero(outgoing.get("amount"))
                difference = abs(incoming_amount - outgoing_amount)
                tolerance = max(incoming_amount * Decimal("0.01"), Decimal("0.000001"))
                if difference <= tolerance:
                    candidates.append(
                        {
                            "asset": asset,
                            "incoming_hash": incoming.get("tx_hash") or "",
                            "outgoing_hash": outgoing.get("tx_hash") or "",
                            "incoming_amount": decimal_to_str(incoming_amount),
                            "outgoing_amount": decimal_to_str(outgoing_amount),
                            "minutes_between": round(delta_minutes, 2),
                        }
                    )
                    break

    return {
        "window_minutes": window_minutes,
        "count": len(candidates),
        "examples": candidates[:20],
    }


def build_turnover_analysis(
    *,
    address: str,
    period: dict[str, Any],
    transactions: list[dict[str, Any]],
    balances: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    valid_transactions = [
        tx
        for tx in transactions
        if not tx.get("is_error") and decimal_or_zero(tx.get("amount")) > 0
    ]
    by_asset: dict[tuple[str, str], dict[str, Any]] = {}
    by_month: dict[tuple[str, str], dict[str, Any]] = {}
    by_day: dict[tuple[str, str], dict[str, Any]] = {}
    counterparty_addresses: set[str] = set()

    for tx in valid_transactions:
        timestamp = tx_datetime(tx)
        if not timestamp:
            continue
        asset = str(tx.get("asset") or "UNKNOWN").upper()
        add_turnover_row(by_asset, ("total", asset), tx, "Итого")
        month_key = f"{timestamp.year}-{timestamp.month:02d}"
        day_key = timestamp.date().isoformat()
        add_turnover_row(by_month, (month_key, asset), tx, month_key)
        add_turnover_row(by_day, (day_key, asset), tx, day_key)
        from_address = str(tx.get("from_address") or "")
        to_address = str(tx.get("to_address") or "")
        counterparty = from_address if tx.get("direction") == "incoming" else to_address
        if counterparty:
            counterparty_addresses.add(counterparty.lower())

    asset_rows = [serialize_turnover_row(row) for row in by_asset.values()]
    asset_rows.sort(key=lambda row: row["asset"])
    monthly_rows = [serialize_turnover_row(row) for row in by_month.values()]
    monthly_rows.sort(key=lambda row: (row["period"], row["asset"]))
    daily_rows = [serialize_turnover_row(row) for row in by_day.values()]
    daily_rows.sort(key=lambda row: (row["period"], row["asset"]))
    activity = build_wallet_activity(valid_transactions)
    rapid_transit = build_rapid_transit(valid_transactions)

    amount_matching: list[dict[str, Any]] = []
    transit_assets: list[str] = []
    for row in asset_rows:
        volume_in = decimal_or_zero(row["volume_in"])
        volume_out = decimal_or_zero(row["volume_out"])
        diff = abs(volume_in - volume_out)
        base = max(volume_in, volume_out, Decimal("1"))
        ratio = diff / base
        is_match = ratio <= Decimal("0.01")
        if is_match and volume_in > 0 and volume_out > 0:
            transit_assets.append(row["asset"])
        amount_matching.append(
            {
                "asset": row["asset"],
                "volume_in": row["volume_in"],
                "volume_out": row["volume_out"],
                "difference": decimal_to_str(diff),
                "difference_ratio": decimal_to_str(ratio, 6),
                "matched": is_match,
            }
        )

    if not valid_transactions:
        conclusion = "По выбранному периоду активность не обнаружена."
    elif transit_assets and (rapid_transit["count"] > 0 or activity.get("active_days", 0) <= 3):
        conclusion = f"Адрес похож на транзитный по активам: {', '.join(transit_assets)}."
    elif transit_assets:
        conclusion = f"Входящие и исходящие суммы близко совпадают по активам: {', '.join(transit_assets)}."
    else:
        conclusion = "Транзитный характер не подтверждён только по суммам; требуется ручная проверка контекста."

    return {
        "address": address,
        "period": period,
        "assets": asset_rows,
        "by_month": monthly_rows,
        "by_day": daily_rows,
        "balance_at_period_end": [
            {
                "asset": row["asset"],
                "amount": row["net_flow"],
                "amount_usd": row["net_flow_usd"],
                "basis": "period_net_flow",
            }
            for row in asset_rows
        ],
        "current_balances": balances or [],
        "activity": activity,
        "first_transaction": activity.get("first_seen"),
        "last_transaction": activity.get("last_seen"),
        "unique_counterparties": len(counterparty_addresses),
        "amount_matching": amount_matching,
        "rapid_transit": rapid_transit,
        "conclusion": conclusion,
    }


def build_common_wallet_report(
    *,
    network: str,
    address: str,
    source: str,
    period: dict[str, Any],
    transactions: list[dict[str, Any]],
    balances: list[dict[str, Any]] | None = None,
    truncated: bool = False,
    counts_extra: dict[str, Any] | None = None,
    fetched_at: str | None = None,
) -> dict[str, Any]:
    summary = build_summary(transactions)
    counterparties = build_counterparties(address, transactions)
    frequency = build_operation_frequency(transactions)
    counts = build_wallet_counts(transactions)
    if counts_extra:
        counts.update(counts_extra)
    normalized_transactions = [
        normalize_transaction_record(tx, chain=network, wallet_address=address)
        for tx in transactions
    ]
    return {
        "network": network,
        "address": address,
        "source": source,
        "period": period,
        "balances": balances or build_balances_from_summary(summary),
        "transactions": transactions,
        "normalized_transactions": normalized_transactions,
        "summary": summary,
        "turnover": build_turnover_analysis(
            address=address,
            period=period,
            transactions=transactions,
            balances=balances,
        ),
        "activity": build_wallet_activity(transactions),
        "counts": counts,
        "counterparties": counterparties,
        "frequency": frequency,
        "suspicious_patterns": detect_suspicious_patterns(transactions, counterparties, frequency),
        "truncated": truncated,
        "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }



def build_summary(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for tx in transactions:
        if tx.get("is_error"):
            continue
        amount = decimal_or_zero(tx.get("amount"))
        if amount <= 0:
            continue
        asset = tx.get("asset") or "UNKNOWN"
        row = summary.setdefault(
            asset,
            {
                "asset": asset,
                "volume_in": Decimal("0"),
                "volume_out": Decimal("0"),
                "volume_total": Decimal("0"),
                "net_flow": Decimal("0"),
                "volume_total_usdt": Decimal("0"),
                "tx_count_in": 0,
                "tx_count_out": 0,
                "tx_count_total": 0,
            },
        )
        usd = decimal_or_zero(tx.get("usd_value"))
        if tx.get("direction") == "incoming":
            row["volume_in"] += amount
            row["net_flow"] += amount
            row["tx_count_in"] += 1
        else:
            row["volume_out"] += amount
            row["net_flow"] -= amount
            row["tx_count_out"] += 1
        row["volume_total"] += amount
        row["volume_total_usdt"] += usd
        row["tx_count_total"] += 1

    return [
        {
            **row,
            "volume_in": decimal_to_str(row["volume_in"]),
            "volume_out": decimal_to_str(row["volume_out"]),
            "volume_total": decimal_to_str(row["volume_total"]),
            "net_flow": decimal_to_str(row["net_flow"]),
            "volume_total_usdt": decimal_to_str(row["volume_total_usdt"], 6),
        }
        for row in sorted(summary.values(), key=lambda item: item["asset"])
    ]

