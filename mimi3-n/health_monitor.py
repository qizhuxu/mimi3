"""
health_monitor — 周期健康检查，检出死 claw。

L3（CF_API_TOKEN 配了 + 账号有 connector_id）：is_replica_active(connector_id) 权威判定。
退化（无 L3）：查云端 instance status + remain——测不出 cloudflared 崩，只测实例在不在。
manager 启动时若 N≥8 且无 L3，log WARN 提示检出延迟可达一个调度周期。
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

from claw_client import BASE_URL, _aistudio_headers
from account_store import AccountState


async def probe_status(cookies: dict, logger: Optional[logging.Logger] = None) -> tuple[str, int, int]:
    """自带 HTTP code 的状态探测。返回 (status, remain_sec, http_code)。
    401=cookie 失效。与 claw_deployer._probe_status 同逻辑（独立副本，避免动已验证的 deployer）。"""
    url = f"{BASE_URL}/open-apis/user/mimo-claw/status"
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(url, cookies=cookies, headers=_aistudio_headers(), timeout=15)
    except Exception as e:
        if logger:
            logger.warning(f"probe_status 网络异常: {e}")
        return "", 0, 0
    if r.status_code == 401:
        return "", 0, 401
    try:
        data = r.json()
    except Exception:
        return "", 0, r.status_code
    d = data.get("data") or {}
    st = str(d.get("status", "")).strip()
    expire_ms = d.get("expireTime")
    remain = max(0, int(int(expire_ms) / 1000 - time.time())) if expire_ms else 0
    return st, remain, r.status_code


class HealthMonitor:
    def __init__(self, tunnel_health=None, interval: int = 300,
                 logger: Optional[logging.Logger] = None):
        self._tunnel_health = tunnel_health
        self.interval = interval
        self._logger = logger or logging.getLogger("health-monitor")
        self._last_check: dict[str, float] = {}
        self._l3_enabled = tunnel_health is not None

    def l3_enabled(self) -> bool:
        return self._l3_enabled

    def should_check(self, uid: str, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        last = self._last_check.get(uid, 0)
        return now - last >= self.interval

    async def check(self, state: AccountState, cookies: dict) -> tuple[bool, str]:
        """返回 (alive, detail)。alive=True=claw 真活着。"""
        self._last_check[state.uid] = time.time()
        # L3：connector_id 在 Cloudflare 活跃列表里
        if self._tunnel_health is not None and state.connector_id:
            try:
                active = await self._tunnel_health.is_replica_active(state.connector_id)
                return active, f"L3 connector_id={state.connector_id[:8]}... active={active}"
            except Exception as e:
                self._logger.warning(f"[{state.uid}] L3 异常降级: {e}")
                # 落到退化路径
        # 退化：云端 instance status + remain
        st, remain, http = await probe_status(cookies, self._logger)
        if http == 401:
            return False, "退化:status 401 cookie 失效"
        alive = (st == "AVAILABLE" and remain > 0)
        return alive, f"退化:status={st!r} remain={remain}s http={http}"

    async def check_public_endpoint(self, public_hostname: str, proxy_api_key: Optional[str]) -> bool:
        """共享域名探测（弱）：curl https://$host/v1/models。只证某 replica 活，不证 per-claw。"""
        if not public_hostname:
            return False
        try:
            headers = {"Authorization": f"Bearer {proxy_api_key}"} if proxy_api_key else {}
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"https://{public_hostname}/v1/models", headers=headers)
            return r.status_code == 200
        except Exception as e:
            self._logger.warning(f"公网探测异常: {e}")
            return False
