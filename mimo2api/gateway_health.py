from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

try:
    from .runtime_config import effective_ws_url, get_config_value
except ImportError:
    from runtime_config import effective_ws_url, get_config_value


@dataclass(frozen=True, slots=True)
class NodePresence:
    uid: str
    source: str
    connected_at: float | None = None
    last_seen_at: float | None = None
    source_url: str = ""
    available: bool = True


def _number_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def stats_url_from_ws_url(ws_url: str) -> str:
    parsed = urlsplit(str(ws_url or "").strip())
    if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
        return ""
    scheme = "https" if parsed.scheme == "wss" else "http"
    return urlunsplit((scheme, parsed.netloc, "/api/stats", "", ""))


def parse_stats_nodes(payload: dict[str, Any], *, source_url: str) -> dict[str, NodePresence]:
    nodes: dict[str, NodePresence] = {}
    raw_nodes = payload.get("nodes", []) if isinstance(payload, dict) else []
    if not isinstance(raw_nodes, list):
        return nodes

    for item in raw_nodes:
        if not isinstance(item, dict):
            continue
        uid = str(item.get("node") or item.get("node_id") or item.get("uid") or "").strip()
        if not uid or uid.lower() in {"unknown", "<未自报>"}:
            continue
        nodes[uid] = NodePresence(
            uid=uid,
            source="remote",
            connected_at=_number_or_none(item.get("connected_at") or item.get("node_connected_at")),
            last_seen_at=_number_or_none(item.get("last_seen_at") or item.get("node_last_seen_at")),
            source_url=source_url,
            available=bool(item.get("available", True)),
        )
    return nodes


def summarize_stats_payload(payload: dict[str, Any]) -> dict[str, int]:
    raw_nodes = payload.get("nodes", []) if isinstance(payload, dict) else []
    if not isinstance(raw_nodes, list):
        raw_nodes = []
    active_clients = payload.get("active_clients")
    try:
        active_count = int(active_clients)
    except (TypeError, ValueError):
        active_count = len(raw_nodes)

    identified = 0
    unknown = 0
    for item in raw_nodes:
        if not isinstance(item, dict):
            unknown += 1
            continue
        uid = str(item.get("node") or item.get("node_id") or item.get("uid") or "").strip()
        if uid and uid.lower() not in {"unknown", "<未自报>"}:
            identified += 1
        else:
            unknown += 1
    if active_count > identified + unknown:
        unknown += active_count - identified - unknown
    return {
        "active_clients": active_count,
        "identified_nodes": identified,
        "unknown_nodes": unknown,
    }


async def fetch_remote_gateway_nodes() -> tuple[dict[str, NodePresence], dict[str, str]]:
    ws_url = effective_ws_url()
    stats_url = stats_url_from_ws_url(ws_url)
    meta = {"ws_url": ws_url, "url": stats_url, "error": ""}
    if not stats_url:
        meta["error"] = "cannot derive /api/stats from WS url"
        return {}, meta

    try:
        client_kwargs: dict[str, Any] = {"timeout": 4}
        stats_proxy = str(get_config_value("gateway.stats_proxy", "") or "").strip()
        if stats_proxy:
            client_kwargs["proxy"] = stats_proxy
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.get(stats_url)
        response.raise_for_status()
        payload = response.json()
        meta.update({key: str(value) for key, value in summarize_stats_payload(payload).items()})
        return parse_stats_nodes(payload, source_url=stats_url), meta
    except Exception as exc:
        meta["error"] = str(exc)[:240]
        return {}, meta
