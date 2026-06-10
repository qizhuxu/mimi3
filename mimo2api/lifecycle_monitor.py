import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

from .gateway_health import NodePresence, fetch_remote_gateway_nodes
from .gateway_state import state
from .runtime_config import get_config_value


ROOT_DIR = Path(__file__).resolve().parent.parent
USERS_DIR = ROOT_DIR / "users"
BASE_URL = "https://aistudio.xiaomimimo.com"

_auto_rebuild_last_at = 0.0


def _aistudio_headers() -> dict[str, str]:
    return {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "User-Agent": "Mozilla/5.0",
    }


def load_users() -> dict[str, dict[str, Any]]:
    users: dict[str, dict[str, Any]] = {}
    if not USERS_DIR.exists():
        return users
    for path in USERS_DIR.glob("user_*.json"):
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        uid = str(data.get("userId", "")).strip()
        if uid:
            users[uid] = data
    return users


async def _fetch_cloud_status(client: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
    uid = str(data.get("userId", "")).strip()
    cookies = {
        "serviceToken": data.get("serviceToken", ""),
        "userId": uid,
        "xiaomichatbot_ph": data.get("xiaomichatbot_ph", ""),
    }
    url = f"{BASE_URL}/open-apis/user/mimo-claw/status"
    try:
        response = await client.get(url, cookies=cookies, headers=_aistudio_headers(), timeout=8)
        if response.status_code == 401:
            return {"cloud_status": "EXPIRED(401)", "remain_sec": 0, "last_error": "credential expired"}
        response.raise_for_status()
        payload = response.json()
        data_obj = payload.get("data") if isinstance(payload, dict) else {}
        status = str((data_obj or {}).get("status") or "UNKNOWN").strip() or "UNKNOWN"
        expire_ms = (data_obj or {}).get("expireTime")
        remain_sec = max(0, int(int(expire_ms) / 1000 - time.time())) if expire_ms else 0
        return {"cloud_status": status, "remain_sec": remain_sec, "last_error": ""}
    except Exception as exc:
        return {"cloud_status": "ERROR", "remain_sec": 0, "last_error": str(exc)[:300]}


def _pending_count_for_ws(ws_id: int) -> int:
    return sum(1 for owner_ws_id in state.req_id_to_ws_id.values() if owner_ws_id == ws_id)


def _cooldown_remaining(ws_id: int, now: float) -> int:
    until = float(state.client_cooldowns.get(ws_id, 0) or 0)
    return max(0, int(until - now))


def _status_display(status: str) -> str:
    return {
        "not_configured": "未配置",
        "credential_expired": "凭证失效",
        "cloud_destroyed": "云端已销毁",
        "cloud_unavailable": "云端不可用",
        "cloud_available_bridge_missing": "云端可用但未接入",
        "bridge_online": "健康",
        "bridge_stale": "桥接失联",
        "bridge_ambiguous": "外网节点未识别",
        "cooling_down": "冷却中",
        "expiring_soon": "即将过期",
        "rebuild_pending": "重建中",
        "unknown": "未知",
    }.get(status, "未知")


def _rebuild_pending() -> bool:
    try:
        from .manager import rebuild_event

        return rebuild_event.is_set()
    except Exception:
        return False


def classify_lifecycle(
    *,
    cloud_status: str,
    remain_sec: int,
    bridge_status: str,
    cooldown_remaining_seconds: int,
    stale: bool,
    has_credentials: bool,
) -> str:
    if not has_credentials:
        return "not_configured"
    if cloud_status == "EXPIRED(401)":
        return "credential_expired"
    if _rebuild_pending():
        return "rebuild_pending"
    if cooldown_remaining_seconds > 0:
        return "cooling_down"
    if bridge_status == "ambiguous":
        return "bridge_ambiguous"
    if bridge_status == "stale" or (bridge_status == "online" and stale):
        return "bridge_stale"
    if bridge_status == "online":
        return "expiring_soon" if 0 < remain_sec <= 300 else "bridge_online"
    if cloud_status == "AVAILABLE":
        return "cloud_available_bridge_missing"
    if cloud_status == "DESTROYED":
        return "cloud_destroyed"
    if cloud_status and cloud_status not in {"UNKNOWN", "ERROR"}:
        return "cloud_unavailable"
    return "unknown"


def resolve_bridge_presence(
    uid: str,
    *,
    remote_nodes: dict[str, NodePresence] | None = None,
    remote_meta: dict[str, Any] | None = None,
    now: float | None = None,
    node_stale_seconds: int = 90,
) -> dict[str, Any]:
    current_time = time.time() if now is None else now
    remote_nodes = remote_nodes or {}
    remote_meta = remote_meta or {}
    ws = state.node_to_ws.get(uid)
    remote = remote_nodes.get(uid)

    ws_id = id(ws) if ws is not None else None
    connected_at = state.node_connected_at.get(uid)
    last_seen_at = state.node_last_seen_at.get(uid)
    sources: list[str] = []
    if ws is not None:
        sources.append("local")
    if remote is not None:
        sources.append("remote")
        connected_at = connected_at or remote.connected_at
        last_seen_at = last_seen_at or remote.last_seen_at

    remote_unknown = 0
    try:
        remote_unknown = int(remote_meta.get("unknown_nodes", 0) or 0)
    except (TypeError, ValueError):
        remote_unknown = 0
    ambiguous = ws is None and remote is None and remote_unknown > 0
    online = ws is not None or remote is not None
    stale = bool(online and last_seen_at and current_time - last_seen_at > node_stale_seconds)
    bridge_status = "offline"
    if online:
        bridge_status = "stale" if stale else "online"
    elif ambiguous:
        bridge_status = "ambiguous"

    return {
        "ws": ws,
        "ws_id": ws_id,
        "online": online,
        "bridge_status": bridge_status,
        "bridge_source": "+".join(sources) if sources else ("remote_unknown" if ambiguous else "none"),
        "node_connected_at": connected_at,
        "node_last_seen_at": last_seen_at,
        "stale": stale,
        "remote_gateway_url": remote.source_url if remote is not None else "",
    }


async def refresh_lifecycle_once() -> dict[str, Any]:
    users = load_users()
    now = time.time()
    node_stale_seconds = int(get_config_value("lifecycle.node_stale_seconds", 90) or 90)
    rows: list[dict[str, Any]] = []
    remote_nodes, remote_meta = await fetch_remote_gateway_nodes()

    async with httpx.AsyncClient() as client:
        status_results = await asyncio.gather(
            *[_fetch_cloud_status(client, data) for data in users.values()],
            return_exceptions=True,
        )

    for (uid, user_data), result in zip(users.items(), status_results):
        if isinstance(result, Exception):
            cloud = {"cloud_status": "ERROR", "remain_sec": 0, "last_error": str(result)[:300]}
        else:
            cloud = result

        presence = resolve_bridge_presence(
            uid,
            remote_nodes=remote_nodes,
            remote_meta=remote_meta,
            now=now,
            node_stale_seconds=node_stale_seconds,
        )
        ws_id = presence["ws_id"]

        cooldown = _cooldown_remaining(ws_id, now) if ws_id is not None else 0
        lifecycle_status = classify_lifecycle(
            cloud_status=str(cloud.get("cloud_status") or "UNKNOWN"),
            remain_sec=int(cloud.get("remain_sec") or 0),
            bridge_status=presence["bridge_status"],
            cooldown_remaining_seconds=cooldown,
            stale=bool(presence["stale"]),
            has_credentials=bool(user_data.get("serviceToken") and user_data.get("xiaomichatbot_ph")),
        )
        previous = state.account_lifecycle.get(uid, {})
        is_failure = lifecycle_status not in {"bridge_online", "expiring_soon", "bridge_ambiguous"}
        consecutive_failures = int(previous.get("consecutive_failures", 0) or 0) + 1 if is_failure else 0

        rows.append({
            "uid": uid,
            "name": user_data.get("name") or f"Imported_{uid}",
            "cloud_status": cloud.get("cloud_status") or "UNKNOWN",
            "remain_sec": int(cloud.get("remain_sec") or 0),
            "bridge_status": presence["bridge_status"],
            "bridge_source": presence["bridge_source"],
            "remote_gateway_url": presence["remote_gateway_url"],
            "lifecycle_status": lifecycle_status,
            "display_status": _status_display(lifecycle_status),
            "node_connected_at": presence["node_connected_at"],
            "node_last_seen_at": presence["node_last_seen_at"],
            "pending_requests": _pending_count_for_ws(ws_id) if ws_id is not None else 0,
            "cooldown_remaining_seconds": cooldown,
            "consecutive_failures": consecutive_failures,
            "last_error": cloud.get("last_error") or "",
        })

    rows.sort(key=lambda item: item["uid"])
    state.account_lifecycle = {row["uid"]: row for row in rows}
    state.node_lifecycle = {
        uid: {
            "connected_at": state.node_connected_at.get(uid),
            "last_seen_at": state.node_last_seen_at.get(uid),
            "online": uid in state.node_to_ws or uid in remote_nodes,
            "source": "local+remote" if uid in state.node_to_ws and uid in remote_nodes else ("remote" if uid in remote_nodes else "local"),
        }
        for uid in set(state.node_to_ws) | set(state.node_last_seen_at) | set(remote_nodes)
    }
    state.remote_gateway = remote_meta
    state.lifecycle_last_refreshed_at = now
    snapshot = build_lifecycle_snapshot()
    await maybe_trigger_auto_rebuild(snapshot)
    return snapshot


def build_lifecycle_snapshot() -> dict[str, Any]:
    rows = list(state.account_lifecycle.values())
    abnormal = [
        row for row in rows
        if row.get("lifecycle_status") not in {"bridge_online", "expiring_soon"}
    ]
    online_nodes = len({
        row.get("uid")
        for row in rows
        if row.get("bridge_status") in {"online", "stale"}
    }) if rows else len(state.node_to_ws)
    return {
        "generated_at": state.lifecycle_last_refreshed_at,
        "accounts": rows,
        "remote_gateway": getattr(state, "remote_gateway", {}),
        "summary": {
            "accounts_total": len(rows),
            "healthy": sum(1 for row in rows if row.get("lifecycle_status") == "bridge_online"),
            "expiring_soon": sum(1 for row in rows if row.get("lifecycle_status") == "expiring_soon"),
            "abnormal": len(abnormal),
            "online_nodes": online_nodes,
            "pending_requests": len(state.pending_queues),
            "cooling_down": sum(1 for row in rows if row.get("cooldown_remaining_seconds", 0) > 0),
        },
    }


async def maybe_trigger_auto_rebuild(snapshot: dict[str, Any]) -> None:
    global _auto_rebuild_last_at
    if not bool(get_config_value("lifecycle.auto_rebuild", False)):
        return
    threshold = max(1, int(get_config_value("lifecycle.auto_rebuild_failures", 3) or 3))
    now = time.time()
    if now - _auto_rebuild_last_at < 300:
        return
    for row in snapshot.get("accounts", []):
        if int(row.get("consecutive_failures", 0) or 0) >= threshold:
            from .manager import trigger_rebuild

            trigger_rebuild()
            _auto_rebuild_last_at = now
            return


async def lifecycle_monitor_worker() -> None:
    while True:
        try:
            await refresh_lifecycle_once()
            interval = max(5, int(get_config_value("lifecycle.monitor_interval_seconds", 30) or 30))
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(10)
