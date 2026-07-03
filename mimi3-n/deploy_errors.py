"""
deploy_errors — 错误类型枚举 + 分类器。

错误分类表见 design.md §3。分类器入口被 ClawDeployer 各阶段调用，决定终态/重试/换策略。
所有 error_type 是 str（便于 JSON 序列化到 DeployResult）。
"""

from __future__ import annotations

import re
from typing import Optional

# ----------------- 错误类型常量 -----------------

# cookie 失效——任意 HTTP 401 或 WS 鉴权拒绝。终态，不重试，标记账号待重登录。
AUTH_EXPIRED = "auth_expired"
# code=7001 创建限流（每小时 1 次）。只复用不重发创建。
CREATE_RATE_LIMITED = "create_rate_limited"
# status=CREATE_FAILED/DESTROYED/ERROR。账号无 Claw 权限，长冷却。
CREATE_FAILED = "create_failed"
# WS res error PROTOCOL_MISMATCH。配置错（需改代码），不重试。
PROTOCOL_MISMATCH = "protocol_mismatch"
# websockets.connect 异常。退避重试。
WS_CONNECT_FAILED = "ws_connect_failed"
# 5s 内无 hello-ok。重新 ticket+connect。
HANDSHAKE_TIMEOUT = "handshake_timeout"
# ticket 接口 400。_get_ticket 内置 5×3s 已处理。
TICKET_SYNC_DELAY = "ticket_sync_delay"
# send_message 超时无 final。退避重试 1 次（防 Claw 仍在跑工具）。
SEND_TIMEOUT = "send_timeout"
# send 中 WS 断。回 ws_handshake 重连+重发。
WS_DISCONNECTED = "ws_disconnected"
# httpx/websockets 抛异常。指数退避。
NETWORK_ERROR = "network_error"
# reply 含 REFUSED/DEPENDENCY_MISSING/手动安装。换注入 prompt 重试。
DEPLOY_REFUSED = "deploy_refused"
# reply 无成功标记但无明确拒绝。重试 1 次后标记人工看。
VERIFY_FAILED = "verify_failed"
# reply 含成功标记。
SUCCESS = "success"

ALL = frozenset({
    AUTH_EXPIRED, CREATE_RATE_LIMITED, CREATE_FAILED, PROTOCOL_MISMATCH,
    WS_CONNECT_FAILED, HANDSHAKE_TIMEOUT, TICKET_SYNC_DELAY, SEND_TIMEOUT,
    WS_DISCONNECTED, NETWORK_ERROR, DEPLOY_REFUSED, VERIFY_FAILED, SUCCESS,
})

# 终态：不重试（除非换账号/换策略后重新进入）
_TERMINAL = frozenset({AUTH_EXPIRED, CREATE_FAILED, PROTOCOL_MISMATCH, SUCCESS})
# 可退避重试（同阶段同策略）
_RETRYABLE = frozenset({
    WS_CONNECT_FAILED, HANDSHAKE_TIMEOUT, TICKET_SYNC_DELAY,
    SEND_TIMEOUT, WS_DISCONNECTED, NETWORK_ERROR,
})
# 换策略重试（deployer 内部换模板 / 调度器换账号）
_STRATEGY_SWAP = frozenset({DEPLOY_REFUSED, VERIFY_FAILED, CREATE_RATE_LIMITED})


def is_terminal(et: Optional[str]) -> bool:
    return et in _TERMINAL

def is_retryable(et: Optional[str]) -> bool:
    return et in _RETRYABLE

def is_strategy_swap(et: Optional[str]) -> bool:
    return et in _STRATEGY_SWAP

def needs_relogin(et: Optional[str]) -> bool:
    """cookie 失效 → Step 3 调度器触发重新取 cookie。"""
    return et == AUTH_EXPIRED


# ----------------- 分类器 -----------------

# cloudflared connector id 行：Generated Connector ID: <uuid>
_CONNECTOR_ID_RE = re.compile(r"Connector ID[:\s]*([0-9a-fA-F-]{36})")

# 成功标记（reply 里出现即认定部署成功，L1）。强标记：只在明确部署成功的汇报里出现。
# 注意：bare "✅" 不算——拒绝/装饰消息也会带 ✅，太弱会误判。
_SUCCESS_MARKERS = (
    "部署完成", "全部通过", "全部验证", "HTTP 200", "200 ✅",
    "Registered tunnel connection", "Tunnel 注册成功",
)
# 拒绝标记（reply 里出现 → deploy_refused，换模板重试）
_REFUSED_MARKERS = (
    "REFUSED", "DEPENDENCY_MISSING", "手动安装", "拒绝", "权限不足",
    "无法安装", "guardrail", "Invalid tunnel secret",
)
# 7001 限流
_RATE_LIMITED_CODE = 7001


def classify_http_error(status: int, code: Optional[int] = None) -> Optional[str]:
    """HTTP 响应分类。返回 error_type 或 None（非错误）。

    优先级：401 > 7001 > 429 > ticket 400 > 其他 4xx/5xx。
    """
    if status == 401:
        return AUTH_EXPIRED
    if code == _RATE_LIMITED_CODE:
        return CREATE_RATE_LIMITED
    if status == 429:
        return CREATE_RATE_LIMITED
    if status == 400:
        # ticket 接口 400 = 同步延迟，_get_ticket 内置重试已处理
        return TICKET_SYNC_DELAY
    if status >= 500:
        return NETWORK_ERROR
    if status >= 402 and status < 500:
        # 403/404/409 等不归类为 auth，交给调用方按上下文处理
        return None
    return None


def classify_instance_status(status: str) -> Optional[str]:
    """实例状态分类（pre-create 探测用）。status 来自 get_instance_status 或创建轮询。

    DESTROYED = 旧实例过期/销毁，账号可创建新的（非错误）；
    仅 *FAILED / ERROR 视为账号级创建失败。
    """
    if not status:
        return None
    if status == "AVAILABLE":
        return SUCCESS
    if status.endswith("FAILED") or status == "ERROR":
        return CREATE_FAILED
    # DESTROYED / CREATING / NOT_CREATED / 其他 → 非终态，交给调用方处理
    return None


def classify_ws_error(exc: BaseException, *, phase: str = "") -> str:
    """WS 异常分类。phase 用于区分 handshake/send 阶段的 Timeout 归属。"""
    msg = str(exc)
    # 协议不匹配（minProtocol/maxProtocol 配置错）
    if "PROTOCOL_MISMATCH" in msg or "protocol" in msg.lower() and "mismatch" in msg.lower():
        return PROTOCOL_MISMATCH
    # 鉴权拒绝（cookie 失效在 WS 层的表现）
    if "Unauthorized" in msg or "401" in msg or "auth" in msg.lower() and "denied" in msg.lower():
        return AUTH_EXPIRED
    # WS 断连
    cls_name = type(exc).__name__
    if "ConnectionClosed" in cls_name or "ConnectionClosed" in msg:
        return WS_DISCONNECTED
    if "ConnectionRefused" in cls_name or "ConnectionRefused" in msg:
        return WS_CONNECT_FAILED
    # 超时：handshake 阶段→handshake_timeout，send 阶段→send_timeout
    if isinstance(exc, (TimeoutError, asyncio_TimeoutError)) or "timeout" in msg.lower() or "Timeout" in cls_name:
        return SEND_TIMEOUT if phase == "send" else HANDSHAKE_TIMEOUT
    # 其他异常归网络错误
    return NETWORK_ERROR


def classify_reply(reply: str) -> str:
    """回复分类。强成功标记优先→success；否则拒绝标记→deploy_refused；无标记→verify_failed。

    成功优先（而非拒绝优先）：claw 成功汇报常含 "Invalid tunnel secret | 无 ✅" 这类
    自检行（表示"未出现该错误"），会误中 _REFUSED_MARKERS 里的 "Invalid tunnel secret"。
    强成功标记（"部署完成"/"全部通过" 等）足以压过这类自检行。bare ✅ 已从成功标记移除，
    故拒绝消息里即使带 ✅ 装饰也不会误判 success。
    """
    if not reply:
        return VERIFY_FAILED
    for marker in _SUCCESS_MARKERS:
        if marker in reply:
            return SUCCESS
    for marker in _REFUSED_MARKERS:
        if marker in reply:
            return DEPLOY_REFUSED
    return VERIFY_FAILED


def extract_connector_id(reply: str) -> Optional[str]:
    """从 claw 汇报里提取 'Generated Connector ID: <uuid>'。"""
    if not reply:
        return None
    m = _CONNECTOR_ID_RE.search(reply)
    return m.group(1) if m else None


# asyncio.TimeoutError 的引用（避免 import asyncio 的副作用）
try:
    import asyncio
    asyncio_TimeoutError = asyncio.TimeoutError  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    asyncio_TimeoutError = TimeoutError  # type: ignore[assignment]


# ----------------- 自检（python -m deploy_errors）-----------------

if __name__ == "__main__":
    # 快速冒烟测
    assert classify_http_error(401) == AUTH_EXPIRED
    assert classify_http_error(200) is None
    assert classify_http_error(429) == CREATE_RATE_LIMITED
    assert classify_http_error(400) == TICKET_SYNC_DELAY
    assert classify_instance_status("AVAILABLE") == SUCCESS
    assert classify_instance_status("CREATE_FAILED") == CREATE_FAILED
    assert classify_instance_status("ERROR") == CREATE_FAILED
    assert classify_instance_status("DESTROYED") is None  # 旧实例过期，可创建新的
    assert classify_instance_status("CREATING") is None
    assert classify_reply("✅ 部署完成 HTTP 200") == SUCCESS
    assert classify_reply("DEPENDENCY_MISSING pip install") == DEPLOY_REFUSED
    assert classify_reply("莫名其妙") == VERIFY_FAILED
    # 回归：成功汇报里的自检行 "Invalid tunnel secret | 无 ✅" 不得误判 deploy_refused
    assert classify_reply("全部通过，部署完成。Invalid tunnel secret | 无 ✅") == SUCCESS
    assert classify_reply("部署完成。| Invalid tunnel secret | 无 |") == SUCCESS
    # 真正的 Invalid tunnel secret 错误（无成功标记）仍归 deploy_refused
    assert classify_reply("cloudflared 日志出现 Invalid tunnel secret，停止部署") == DEPLOY_REFUSED
    # 拒绝消息带 ✅ 装饰不得误判 success（bare ✅ 已移除）
    assert classify_reply("无法安装 cloudflared ✅ 已尝试") == DEPLOY_REFUSED
    assert extract_connector_id("Generated Connector ID: d8733b5a-7c1c-4a36-9dc7-2e43fbb23693") == "d8733b5a-7c1c-4a36-9dc7-2e43fbb23693"
    assert needs_relogin(AUTH_EXPIRED) is True
    assert is_terminal(SUCCESS) and is_terminal(AUTH_EXPIRED)
    assert is_retryable(NETWORK_ERROR)
    assert is_strategy_swap(DEPLOY_REFUSED)
    print("deploy_errors self-check OK")
