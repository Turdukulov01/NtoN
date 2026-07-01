from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import time
from typing import Any

from fastapi import HTTPException

from app.application.wallets.service import sync_tron_wallet_impl
from app.core.formatting import decimal_or_zero, decimal_to_str
from app.core.settings import TRON_TRACE_ALL_TIME_MAX_ITEMS_PER_WALLET, TRON_TRACE_CANDIDATE_MULTIPLIER, TRON_TRACE_MAX_ADDRESSES, TRON_TRACE_MAX_EDGES, TRON_TRACE_MAX_SECONDS
from app.domain.wallet.addresses import is_tron_address, normalize_tron_address

def same_address(left: Any, right: Any) -> bool:
    return str(left or "").strip().lower() == str(right or "").strip().lower()


def aggregate_transfer_edges(
    address: str,
    transactions: list[dict[str, Any]],
    asset_filter: str,
    max_branches: int,
    direction: str,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    checked_address = address.strip()
    for tx in transactions:
        if tx.get("is_error"):
            continue

        timestamp = str(tx.get("timestamp") or "")
        from_address = str(tx.get("from_address") or "").strip()
        to_address = str(tx.get("to_address") or "").strip()

        if direction == "outgoing":
            if not same_address(from_address, checked_address):
                continue
            from_address = checked_address
        elif direction == "incoming":
            if not same_address(to_address, checked_address):
                continue
            to_address = checked_address
        else:
            continue

        if (
            not is_tron_address(from_address)
            or not is_tron_address(to_address)
            or same_address(from_address, to_address)
        ):
            continue

        amount = decimal_or_zero(tx.get("amount"))
        if amount <= 0:
            continue

        asset = str(tx.get("asset") or "UNKNOWN").upper()
        if asset_filter != "all" and asset != asset_filter:
            continue

        key = (from_address.lower(), to_address.lower(), asset)
        row = groups.setdefault(
            key,
            {
                "from": from_address,
                "to": to_address,
                "direction": direction,
                "asset": asset,
                "amount": Decimal("0"),
                "usd_value": Decimal("0"),
                "tx_count": 0,
                "methods": set(),
                "tx_types": set(),
                "hashes": [],
                "first_seen": "",
                "last_seen": "",
            },
        )
        row["amount"] += amount
        row["usd_value"] += decimal_or_zero(tx.get("usd_value"))
        row["tx_count"] += 1
        if tx.get("method"):
            row["methods"].add(str(tx["method"]))
        if tx.get("tx_type"):
            row["tx_types"].add(str(tx["tx_type"]))
        tx_hash = tx.get("tx_hash")
        if tx_hash and tx_hash not in row["hashes"] and len(row["hashes"]) < 20:
            row["hashes"].append(tx_hash)

        if timestamp:
            if not row["first_seen"] or timestamp < row["first_seen"]:
                row["first_seen"] = timestamp
            if not row["last_seen"] or timestamp > row["last_seen"]:
                row["last_seen"] = timestamp

    sorted_rows = sorted(
        groups.values(),
        key=lambda item: (item["usd_value"], item["amount"], item["tx_count"]),
        reverse=True,
    )
    if max_branches > 0:
        sorted_rows = sorted_rows[:max_branches]

    for row in sorted_rows:
        row["methods"] = sorted(row["methods"])
        row["tx_types"] = sorted(row["tx_types"])

    return sorted_rows


def add_trace_incoming(node: dict[str, Any], asset: str, amount: Decimal, usd_value: Decimal) -> None:
    incoming = node.setdefault("_incoming_trace", {})
    row = incoming.setdefault(asset, {"asset": asset, "amount": Decimal("0"), "usd_value": Decimal("0")})
    row["amount"] += amount
    row["usd_value"] += usd_value


def serialize_trace_node(node: dict[str, Any]) -> dict[str, Any]:
    incoming_rows = []
    for row in sorted(node.get("_incoming_trace", {}).values(), key=lambda item: item["asset"]):
        incoming_rows.append(
            {
                "asset": row["asset"],
                "amount": decimal_to_str(row["amount"], 6),
                "usd_value": decimal_to_str(row["usd_value"], 6),
            }
        )
    return {
        key: value
        for key, value in {
            **node,
            "incoming_trace": incoming_rows,
        }.items()
        if not key.startswith("_")
    }

async def trace_tron_wallet(
    address: str,
    days: int = 31,
    depth: int = 2,
    asset: str = "all",
    include_incoming: bool = True,
    max_branches: int = 0,
    max_items_per_wallet: int = 500,
    date_from: str | None = None,
    date_to: str | None = None,
):
    root_address = normalize_tron_address(address)
    if not is_tron_address(root_address):
        raise HTTPException(status_code=400, detail="Некорректный TRON-адрес")

    started_at = time.monotonic()

    def trace_time_left() -> bool:
        return time.monotonic() - started_at < TRON_TRACE_MAX_SECONDS

    all_time_trace = days == 0 and not (date_from or date_to)
    if all_time_trace:
        max_items_per_wallet = min(max_items_per_wallet, max(50, TRON_TRACE_ALL_TIME_MAX_ITEMS_PER_WALLET))

    asset_filter = asset.strip().upper() if asset.strip().lower() != "all" else "all"
    max_addresses = max(1, TRON_TRACE_MAX_ADDRESSES)
    max_edges = max(1, TRON_TRACE_MAX_EDGES)
    max_candidate_edges = max_edges * max(1, TRON_TRACE_CANDIDATE_MULTIPLIER)
    nodes: dict[str, dict[str, Any]] = {}
    edge_candidates: list[dict[str, Any]] = []
    edge_keys_seen: set[tuple[str, str, str]] = set()
    visited: set[str] = set()
    queued: set[str] = {root_address.lower()}
    queue: list[tuple[str, int]] = [(root_address, 0)]
    truncated = False
    time_budget_hit = False

    nodes[root_address.lower()] = {
        "id": root_address.lower(),
        "address": root_address,
        "depth": 0,
        "root": True,
        "fetched": False,
        "tx_count": 0,
        "in_edges": 0,
        "out_edges": 0,
        "source_only": False,
        "summary": [],
        "error": "",
    }

    while queue and len(visited) < max_addresses and len(edge_candidates) < max_candidate_edges and trace_time_left():
        current_address, current_depth = queue.pop(0)
        current_key = current_address.lower()
        if current_key in visited:
            continue
        visited.add(current_key)

        node = nodes.setdefault(
            current_key,
            {
                "id": current_key,
                "address": current_address,
                "depth": current_depth,
                "root": False,
                "fetched": False,
                "tx_count": 0,
                "in_edges": 0,
                "out_edges": 0,
                "source_only": False,
                "summary": [],
                "error": "",
            },
        )
        node["depth"] = min(node.get("depth", current_depth), current_depth)

        if not trace_time_left():
            time_budget_hit = True
            truncated = True
            break

        try:
            payload = await sync_tron_wallet_impl(
                address=current_address,
                days=days,
                max_items=max_items_per_wallet,
                date_from=date_from,
                date_to=date_to,
            )
        except HTTPException as exc:
            node["error"] = str(exc.detail)
            continue

        if not trace_time_left():
            time_budget_hit = True
            truncated = True

        node["fetched"] = True
        node["tx_count"] = payload.get("counts", {}).get("total", 0)
        node["summary"] = payload.get("summary", [])
        if payload.get("truncated"):
            truncated = True

        outgoing = aggregate_transfer_edges(
            current_address,
            payload.get("transactions", []),
            asset_filter,
            max_branches,
            "outgoing",
        )
        incoming = (
            aggregate_transfer_edges(
                current_address,
                payload.get("transactions", []),
                asset_filter,
                max_branches,
                "incoming",
            )
            if include_incoming
            else []
        )
        node["in_edges"] = len(incoming)
        node["out_edges"] = len(outgoing)

        for index, edge in enumerate(outgoing):
            if len(edge_candidates) >= max_candidate_edges:
                truncated = True
                break

            from_address = edge["from"]
            from_key = from_address.lower()
            to_address = edge["to"]
            to_key = to_address.lower()
            edge_key = (from_key, to_key, edge["asset"])
            if edge_key in edge_keys_seen:
                continue
            child_depth = current_depth + 1
            existing_child = nodes.get(to_key)
            cycle = bool(existing_child and existing_child.get("depth", child_depth) <= current_depth)
            child = nodes.setdefault(
                to_key,
                {
                    "id": to_key,
                    "address": to_address,
                    "depth": child_depth,
                    "root": False,
                    "fetched": False,
                    "tx_count": 0,
                    "in_edges": 0,
                    "out_edges": 0,
                    "source_only": False,
                    "summary": [],
                    "error": "",
                },
            )
            child["depth"] = min(child.get("depth", child_depth), child_depth)
            child["source_only"] = False
            add_trace_incoming(child, edge["asset"], edge["amount"], edge["usd_value"])
            edge_keys_seen.add(edge_key)

            edge_candidates.append(
                {
                    "id": f"{current_key}:{to_key}:{edge['asset']}:out:{current_depth}:{index}",
                    "from": current_key,
                    "to": to_key,
                    "from_address": from_address,
                    "to_address": to_address,
                    "checked_address": current_address,
                    "direction": "outgoing",
                    "asset": edge["asset"],
                    "amount": decimal_to_str(edge["amount"], 6),
                    "usd_value": decimal_to_str(edge["usd_value"], 6),
                    "tx_count": edge["tx_count"],
                    "methods": edge["methods"],
                    "tx_types": edge["tx_types"],
                    "cycle": cycle,
                    "depth": child_depth,
                    "trace_distance": child_depth,
                    "hashes": edge["hashes"],
                    "first_seen": edge["first_seen"],
                    "last_seen": edge["last_seen"],
                }
            )

            if cycle:
                continue

            if child_depth < depth and to_key not in visited and to_key not in queued:
                if len(visited) + len(queue) >= max_addresses:
                    truncated = True
                    continue
                queue.append((to_address, child_depth))
                queued.add(to_key)

        for index, edge in enumerate(incoming):
            if len(edge_candidates) >= max_candidate_edges:
                truncated = True
                break

            from_address = edge["from"]
            from_key = from_address.lower()
            edge_key = (from_key, current_key, edge["asset"])
            if edge_key in edge_keys_seen:
                continue
            source_depth = current_depth - 1
            existing_source = nodes.get(from_key)
            source = nodes.setdefault(
                from_key,
                {
                    "id": from_key,
                    "address": from_address,
                    "depth": source_depth,
                    "root": False,
                    "fetched": False,
                    "tx_count": 0,
                    "in_edges": 0,
                    "out_edges": 0,
                    "source_only": True,
                    "summary": [],
                    "error": "",
                },
            )
            if existing_source is None:
                source["depth"] = source_depth

            add_trace_incoming(node, edge["asset"], edge["amount"], edge["usd_value"])
            edge_keys_seen.add(edge_key)

            edge_candidates.append(
                {
                    "id": f"{from_key}:{current_key}:{edge['asset']}:in:{current_depth}:{index}",
                    "from": from_key,
                    "to": current_key,
                    "from_address": from_address,
                    "to_address": current_address,
                    "checked_address": current_address,
                    "direction": "incoming",
                    "asset": edge["asset"],
                    "amount": decimal_to_str(edge["amount"], 6),
                    "usd_value": decimal_to_str(edge["usd_value"], 6),
                    "tx_count": edge["tx_count"],
                    "methods": edge["methods"],
                    "tx_types": edge["tx_types"],
                    "cycle": bool(existing_source and existing_source.get("fetched")),
                    "depth": current_depth,
                    "trace_distance": current_depth + 1,
                    "hashes": edge["hashes"],
                    "first_seen": edge["first_seen"],
                    "last_seen": edge["last_seen"],
                }
            )

    if queue:
        truncated = True
    if len(edge_candidates) > max_edges:
        truncated = True
    if not trace_time_left():
        time_budget_hit = True
        truncated = True

    def edge_sort_key(edge: dict[str, Any]) -> tuple[Any, ...]:
        distance = int(edge.get("trace_distance") or edge.get("depth") or 999)
        direction_priority = 0 if edge.get("direction") == "outgoing" else 1
        usd = decimal_or_zero(edge.get("usd_value"))
        amount = decimal_or_zero(edge.get("amount"))
        first_seen = str(edge.get("first_seen") or "")
        return (distance, direction_priority, -usd, -amount, first_seen)

    edges = sorted(edge_candidates, key=edge_sort_key)[:max_edges]
    visible_node_ids = {root_address.lower()}
    for edge in edges:
        visible_node_ids.add(str(edge.get("from") or "").lower())
        visible_node_ids.add(str(edge.get("to") or "").lower())

    return {
        "address": root_address,
        "period": {
            "days": days,
            "all_time": all_time_trace,
            "date_from": date_from,
            "date_to": date_to,
        },
        "depth": depth,
        "asset": asset_filter,
        "include_incoming": include_incoming,
        "max_branches": max_branches,
        "max_addresses": max_addresses,
        "max_edges": max_edges,
        "max_items_per_wallet": max_items_per_wallet,
        "candidate_edges": len(edge_candidates),
        "edge_limit_strategy": "nearest_to_root",
        "time_budget_seconds": TRON_TRACE_MAX_SECONDS,
        "time_budget_hit": time_budget_hit,
        "visited_addresses": len(visited),
        "truncated": truncated,
        "nodes": [serialize_trace_node(node) for node in nodes.values() if node["id"] in visible_node_ids],
        "edges": edges,
        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

trace_wallet_graph = trace_tron_wallet
