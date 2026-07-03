"""
tunnel_health — Cloudflare Tunnel 连接查询（L3 per-claw 连通性验证，可选）。

调 Cloudflare API GET /accounts/{aid}/cfd_tunnel/{tid}/connections 拿活跃连接列表，
按 connector_id 配对本 claw 的 replica 是否真上线——这是共享域名探测做不到的
per-claw 连通性权威判据。

**可选**：仅当用户提供 CF_API_TOKEN 时实例化。未提供则 ClawDeployer 跳过 L3，
靠 L1（reply 本地 HTTP code）+ L2（cloudflared.log connector_id 提取）做弱保证，
部署仍能 success。

API 鉴权：Authorization: Bearer <CF_API_TOKEN>，token 需 Cloudflare Tunnel: Read 权限。
account_id 是 Cloudflare 账号 ID（非 deploy 脚本里的 ACCOUNT_TAG）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from deploy_errors import extract_connector_id  # 复用 reply 提取逻辑

CF_API_BASE = "https://api.cloudflare.com/client/v4"


@dataclass
class TunnelConnection:
    connector_id: str           # client_id 字段，per-claw 唯一
    origin_ip: str              # claw 的公网 IP（辅助识别）
    colo_name: str              # 连的哪个 Cloudflare 数据中心
    is_pending_reconnect: bool  # True=已断开仍被追踪，False=正在服务
    opened_at: str


class TunnelHealth:
    """Cloudflare Tunnel 连接查询。可选 L3 验证。"""

    def __init__(
        self,
        account_id: str,
        tunnel_id: str,
        api_token: str,
        logger: Optional[logging.Logger] = None,
    ):
        if not (account_id and tunnel_id and api_token):
            raise ValueError("TunnelHealth 需要 account_id + tunnel_id + api_token 都非空")
        self.account_id = account_id.strip()
        self.tunnel_id = tunnel_id.strip()
        self.api_token = api_token.strip()
        self._logger = logger or logging.getLogger("tunnel-health")
        self._headers = {"Authorization": f"Bearer {self.api_token}"}

    async def list_connections(self) -> list[TunnelConnection]:
        """GET 拿本 tunnel 的活跃连接列表。失败抛异常（deployer 降级 L3）。"""
        url = f"{CF_API_BASE}/accounts/{self.account_id}/cfd_tunnel/{self.tunnel_id}/connections"
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, headers=self._headers)
        if r.status_code == 403:
            raise PermissionError(f"CF_API_TOKEN 权限不足（需 Tunnel:Read）: {r.status_code}")
        if r.status_code != 200:
            raise RuntimeError(f"Cloudflare API {r.status_code}: {r.text[:200]}")
        body = r.json()
        if not body.get("success"):
            raise RuntimeError(f"Cloudflare API 返回 success=false: {body.get('errors')}")
        result = body.get("result") or []
        out: list[TunnelConnection] = []
        for item in result:
            if not isinstance(item, dict):
                continue
            out.append(TunnelConnection(
                connector_id=str(item.get("client_id", "")),
                origin_ip=str(item.get("origin_ip", "")),
                colo_name=str(item.get("colo", {}).get("name", "") if isinstance(item.get("colo"), dict) else item.get("colo", "")),
                is_pending_reconnect=bool(item.get("is_pending_reconnect", False)),
                opened_at=str(item.get("opened_at", "")),
            ))
        return out

    async def is_replica_active(self, connector_id: str) -> bool:
        """本 connector_id 在活跃列表里且 is_pending_reconnect=False。"""
        if not connector_id:
            return False
        try:
            conns = await self.list_connections()
        except Exception as e:
            self._logger.warning(f"list_connections 失败，L3 降级: {e}")
            return False
        for c in conns:
            if c.connector_id == connector_id:
                return not c.is_pending_reconnect
        return False

    async def active_connector_ids(self) -> set[str]:
        """所有活跃 connector_id（is_pending_reconnect=False）。Step 3 批量配对用。"""
        try:
            conns = await self.list_connections()
        except Exception as e:
            self._logger.warning(f"list_connections 失败: {e}")
            return set()
        return {c.connector_id for c in conns if not c.is_pending_reconnect}

    async def probe(self) -> bool:
        """初始化时探一次：能拉回列表即配置正确（即使列表为空也 OK）。403→False。"""
        try:
            await self.list_connections()
            return True
        except PermissionError as e:
            self._logger.error(f"TunnelHealth 配置错: {e}")
            return False
        except Exception as e:
            self._logger.warning(f"TunnelHealth probe 异常（L3 将降级）: {e}")
            return False


def extract_connector_id_from_reply(reply: str) -> Optional[str]:
    """从 claw 汇报里提取 'Generated Connector ID: <uuid>'。复用 deploy_errors。"""
    return extract_connector_id(reply)


if __name__ == "__main__":
    # 冒烟测（无 token 时只测 extract）
    assert extract_connector_id_from_reply("Generated Connector ID: d8733b5a-7c1c-4a36-9dc7-2e43fbb23693") == "d8733b5a-7c1c-4a36-9dc7-2e43fbb23693"
    assert extract_connector_id_from_reply("无 id") is None
    print("tunnel_health self-check OK (extract only; API 需真实 CF_API_TOKEN)")
