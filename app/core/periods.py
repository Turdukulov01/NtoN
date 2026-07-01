from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from app.core.formatting import decimal_or_zero

def parse_sync_date_boundary(value: str | None, end_of_day: bool) -> datetime | None:
    if not value:
        return None

    text = value.strip()
    if not text:
        return None

    try:
        if len(text) == 10:
            parsed = datetime.fromisoformat(text)
            if end_of_day:
                return datetime(parsed.year, parsed.month, parsed.day, 23, 59, 59, 999000, tzinfo=timezone.utc)
            return datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc)

        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Некорректная дата: {value}") from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_sync_period(
    days: int,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[datetime | None, datetime, dict[str, Any]]:
    now = datetime.now(timezone.utc)
    range_start = parse_sync_date_boundary(date_from, end_of_day=False)
    range_end = parse_sync_date_boundary(date_to, end_of_day=True)

    if range_start or range_end:
        end = range_end or now
        if end > now:
            end = now
        start = range_start
        if start and start > end:
            raise HTTPException(status_code=400, detail="Дата начала не может быть позже даты окончания")
    else:
        end = now
        start = end - timedelta(days=days) if days > 0 else None

    return start, end, {
        "days": days,
        "all_time": days == 0 and not (range_start or range_end),
        "start": start.isoformat().replace("+00:00", "Z") if start else None,
        "end": end.isoformat().replace("+00:00", "Z"),
        "date_from": date_from,
        "date_to": date_to,
    }


def tx_datetime(tx: dict[str, Any]) -> datetime | None:
    raw = str(tx.get("timestamp") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def filter_transactions_by_period(
    transactions: list[dict[str, Any]],
    start: datetime | None,
    end: datetime,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for tx in transactions:
        timestamp = tx_datetime(tx)
        if timestamp is None:
            continue
        if start and timestamp < start:
            continue
        if timestamp > end:
            continue
        filtered.append(tx)
    return filtered
