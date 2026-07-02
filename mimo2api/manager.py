#!/usr/bin/env python3
"""
mimo2api ???????????? (Manager)

??:
1. ?????????????????? (users/ ??)
2. ??????? Claw ????????? 4 ??????????? 1 ??
3. ????????? bridge.py?????????????????
????????????? claw_chat.py ? claw_web.py ??????
"""


import sys
import os
import json
import time
import datetime
import asyncio
import logging
import uuid
import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import quote
import httpx
import websockets

try:
    from .bridge_prompt_store import (
        default_bridge_prompt_templates,
        load_effective_bridge_prompt_templates,
        render_bridge_prompt_text,
    )
    from .gateway_health import fetch_remote_gateway_nodes, is_cloud_reachable_ws_url
    from .logging_utils import compact_text, log_event
    from .runtime_config import get_config_value
except ImportError:
    from bridge_prompt_store import (
        default_bridge_prompt_templates,
        load_effective_bridge_prompt_templates,
        render_bridge_prompt_text,
    )
    from gateway_health import fetch_remote_gateway_nodes, is_cloud_reachable_ws_url
    from logging_utils import compact_text, log_event
    from runtime_config import get_config_value

# ????????
_DAILY_CREATIONS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "daily_creations.json")
_daily_creations_cache: dict[str, dict[str, int]] = {}
_known_manager_uids: set[str] = set()


def _today_str() -> str:
    """???? UTC+8 ??????"""
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y-%m-%d")


def _load_daily_creations() -> dict[str, dict[str, int]]:
    """? data/daily_creations.json ?????????"""
    global _daily_creations_cache
    try:
        if os.path.exists(_DAILY_CREATIONS_PATH):
            with open(_DAILY_CREATIONS_PATH, "r", encoding="utf-8") as f:
                _daily_creations_cache = json.load(f)
    except Exception:
        _daily_creations_cache = {}
    return _daily_creations_cache


def _save_daily_creation(uid: str) -> None:
    """???????????"""
    global _daily_creations_cache
    today = _today_str()
    uid_key = str(uid)
    if uid_key not in _daily_creations_cache:
        _daily_creations_cache[uid_key] = {}
    _daily_creations_cache[uid_key][today] = _daily_creations_cache[uid_key].get(today, 0) + 1
    # ??????????????
    yesterday = (datetime.datetime.utcnow() + datetime.timedelta(hours=8, days=-1)).strftime("%Y-%m-%d")
    _daily_creations_cache[uid_key] = {
        d: c for d, c in _daily_creations_cache[uid_key].items() if d >= yesterday
    }
    try:
        os.makedirs(os.path.dirname(_DAILY_CREATIONS_PATH), exist_ok=True)
        with open(_DAILY_CREATIONS_PATH, "w", encoding="utf-8") as f:
            json.dump(_daily_creations_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"??????????: {e}")


def _can_create_today(uid: str) -> bool:
    """?????????????????"""
    limit = max(1, _config_int("lifecycle.daily_create_limit", 1))
    today = _today_str()
    uid_key = str(uid)
    count = _daily_creations_cache.get(uid_key, {}).get(today, 0)
    return count < limit


def _seconds_until_tomorrow_utc8() -> int:
    """?????? UTC+8 00:00 ????"""
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    tomorrow = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(60, int((tomorrow - now).total_seconds()))


async def cancel_and_wait(tasks: list[asyncio.Task], timeout: float = 5.0) -> None:
    pending = [task for task in tasks if not task.done()]
    if not pending:
        return

    for task in pending:
        task.cancel()

    try:
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(f"取消子任务超时，仍有 {sum(not task.done() for task in pending)} 个任务未退出")


def current_available_nodes(exclude_uid: str | None = None) -> int:
    try:
        from mimo2api.gateway_state import state as gw_state
    except Exception:
        return 0
    now = time.time()
    total = 0
    for node_id, ws in list(gw_state.node_to_ws.items()):
        if exclude_uid is not None and str(node_id) == str(exclude_uid):
            continue
        if gw_state.client_cooldowns.get(id(ws), 0) > now:
            continue
        total += 1
    return total


def _config_int(key: str, default: int) -> int:
    value = get_config_value(key, default)
    if value is None or value == "":
        value = default
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


# 配置日志格式
logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(name)s] - %(levelname)s - %(message)s")
logger = logging.getLogger("Manager")
logging.getLogger("httpx").setLevel(logging.WARNING)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_URL = "https://aistudio.xiaomimimo.com"
WS_URL = "wss://aistudio.xiaomimimo.com/ws/proxy"


@dataclass(frozen=True)
class NodeWaitResult:
    exact: bool = False
    ambiguous: bool = False
    source_url: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.exact or self.ambiguous


def _meta_int(meta: dict, key: str) -> int:
    try:
        return int(meta.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _remote_identity_unavailable(meta: dict) -> bool:
    return _meta_int(meta, "unknown_nodes") > 0 and _meta_int(meta, "identified_nodes") == 0

# ----------------- 用户加载逻辑 (遵循 web_core.py 原版逻辑) -----------------
def load_all_users() -> dict:
    """从 users/ 目录读取所有用户的登录凭证"""
    users = {}
    ud = os.path.join(ROOT_DIR, "users")
    if os.path.exists(ud):
        for fn in os.listdir(ud):
            if fn.startswith("user_") and fn.endswith(".json"):
                try:
                    with open(os.path.join(ud, fn), "r", encoding="utf-8") as f:
                        udata = json.load(f)
                        uid = udata.get("userId")
                        if uid:
                            users[str(uid).strip()] = udata
                except Exception:
                    continue
    return users


async def get_bridge_code(node_id: str | None = None) -> str:
    """读取本地 bridge 代码文本"""
    import re
    bridge_path = os.path.join(os.path.dirname(__file__), "bridge.py")
    def _read():
        with open(bridge_path, "r", encoding="utf-8") as f:
            return f.read()
    code = await asyncio.to_thread(_read)

    # 获取全局 main.py 配置入口配置好的统一穿透通信地址，若缺失则降级 fallback
    try:
        from .runtime_config import sync_bridge_ws_env
    except ImportError:
        from runtime_config import sync_bridge_ws_env

    ws_url = sync_bridge_ws_env()
    if not ws_url:
        raise ValueError("MIMO2API_WS_URL环境变量未配置")
    if not is_cloud_reachable_ws_url(ws_url):
        raise ValueError(f"Bridge WS URL 不可从云端 Claw 回连: {ws_url}")
    # 若提供了 node_id，把它拼到 query string 上，便于网关精准识别该账号节点是否在线
    if node_id:
        sep = "&" if "?" in ws_url else "?"
        ws_url = f"{ws_url}{sep}node={quote(str(node_id))}"
    # 动态把桥接脚本里面原来写死的 WS_URL 给替换掉，并返回修改后的代码块。
    code = code.replace("__WS_URL__", ws_url)
    return code


def _aistudio_headers() -> dict:
    return {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "x-timezone": "Asia/Shanghai",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }


def _truncate_text(value, limit: int = 300) -> str:
    return compact_text(value, limit=limit)


_SECRET_TEXT_SUBS = (
    (re.compile(r"(?i)(cookie\s*:\s*)[^\n\r]+"), r"\1<redacted>"),
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+"), r"\1<redacted>"),
    (re.compile(r"(?i)((?:serviceToken|xiaomichatbot_ph|session_secret|webui_session)\s*=\s*)[\"']?[^;\s,\"']+"), r"\1<redacted>"),
    (re.compile(r"(?i)(\"(?:serviceToken|xiaomichatbot_ph|session_secret|webui_session)\"\s*:\s*\")[^\"]+(\")"), r"\1<redacted>\2"),
    (re.compile(r"(?i)('(?:serviceToken|xiaomichatbot_ph|session_secret|webui_session)'\s*:\s*')[^']+(')"), r"\1<redacted>\2"),
    (re.compile(r"(?i)((?:MIMO_API_KEY|MIMO_API_ENDPOINT)\s*=\s*)[\"']?[^;\s,\"']+"), r"\1<redacted>"),
    (re.compile(r"(?i)(\"(?:MIMO_API_KEY|MIMO_API_ENDPOINT|api-key)\"\s*:\s*\")[^\"]+(\")"), r"\1<redacted>\2"),
    (re.compile(r"(?i)('(?:MIMO_API_KEY|MIMO_API_ENDPOINT|api-key)'\s*:\s*')[^']+(')"), r"\1<redacted>\2"),
)


def safe_claw_trace_text(value, limit: int = 360) -> str:
    text = "" if value is None else str(value)
    for pattern, replacement in _SECRET_TEXT_SUBS:
        text = pattern.sub(replacement, text)
    return compact_text(text, limit=limit)


BRIDGE_REFUSAL_NODE_WAIT_SECONDS = 5


@dataclass(frozen=True)
class BridgeInjectionPrompt:
    prompt_id: str
    text: str
    preferred_after: tuple[str, ...] = ()


_BRIDGE_FAILURE_PRIORITY = (
    "dependency_install_refused",
    "dependency_missing",
    "proxy_or_api_key_refused",
    "manual_action_requested",
    "timeout",
    "generic_refusal",
)


def _append_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def bridge_injection_failure_classes(reply: str | None) -> tuple[str, ...]:
    text = "" if reply is None else str(reply)
    lowered = text.lower()
    classes: list[str] = []

    if not text.strip() or "等待最终态回复超时" in text or "timeout" in lowered or "超时" in text:
        _append_unique(classes, "timeout")

    if (
        "pip install" in lowered
        and any(marker in lowered for marker in ("禁止", "不能", "拒绝", "无法", "不会", "security", "forbid", "refuse"))
    ):
        _append_unique(classes, "dependency_install_refused")

    if any(marker in lowered for marker in (
        "dependency_missing",
        "missing_dependency",
        "dependency_unavailable",
        "missing module",
        "missing modules",
        "module not found",
        "modulenotfounderror",
        "no module named",
        "not installed",
        "missing dependency",
        "dependency missing",
        "dependency unavailable",
    )) or any(marker in text for marker in ("没装", "未安装", "没有安装", "缺少依赖", "缺少模块", "缺失依赖", "缺失模块")):
        _append_unique(classes, "dependency_missing")

    if (
        any(marker in lowered for marker in ("api key", "api-key", "credential", "credentials", "proxy external"))
        or any(marker in text for marker in ("凭据", "密钥", "代理外部", "暴露给第三方", "本机 API Key"))
    ) and _looks_like_refusal(text):
        _append_unique(classes, "proxy_or_api_key_refused")

    if any(marker in text for marker in ("请你手动", "手动运行", "安装完成后告诉我", "完成后告诉我")) or any(
        marker in lowered for marker in ("manual action", "manually run", "tell me when")
    ):
        _append_unique(classes, "manual_action_requested")

    if _looks_like_refusal(text):
        _append_unique(classes, "generic_refusal")

    return tuple(classes)


def classify_bridge_injection_failure(reply: str | None) -> str:
    classes = bridge_injection_failure_classes(reply)
    for failure_class in _BRIDGE_FAILURE_PRIORITY:
        if failure_class in classes:
            return failure_class
    return ""


def _bridge_node_wait_timeout(failure_classes: Sequence[str], default_timeout: int) -> int:
    refusal_classes = {
        "dependency_install_refused",
        "dependency_missing",
        "proxy_or_api_key_refused",
        "manual_action_requested",
        "generic_refusal",
    }
    if any(cls in refusal_classes for cls in failure_classes):
        return min(int(default_timeout), BRIDGE_REFUSAL_NODE_WAIT_SECONDS)
    return int(default_timeout)


def build_bridge_injection_prompt_library(bridge_code: str) -> tuple[BridgeInjectionPrompt, ...]:
    return tuple(
        BridgeInjectionPrompt(
            prompt_id=template.prompt_id,
            text=render_bridge_prompt_text(template, bridge_code),
            preferred_after=tuple(template.preferred_after),
        )
        for template in default_bridge_prompt_templates()
        if template.enabled
    )


def build_effective_bridge_injection_prompt_library(bridge_code: str) -> tuple[BridgeInjectionPrompt, ...]:
    templates = load_effective_bridge_prompt_templates()
    return tuple(
        BridgeInjectionPrompt(
            prompt_id=template.prompt_id,
            text=render_bridge_prompt_text(template, bridge_code),
            preferred_after=tuple(template.preferred_after),
        )
        for template in templates
    )


def _coerce_bridge_prompts(inject_prompt: str | Sequence[BridgeInjectionPrompt]) -> tuple[BridgeInjectionPrompt, ...]:
    if isinstance(inject_prompt, str):
        return (BridgeInjectionPrompt(prompt_id="bridge.v1.legacy", text=inject_prompt),)
    return tuple(inject_prompt)


def select_bridge_injection_prompt(
    prompts: Sequence[BridgeInjectionPrompt],
    *,
    previous_failure_classes: Sequence[str] = (),
    used_prompt_ids: set[str] | None = None,
) -> BridgeInjectionPrompt:
    used = used_prompt_ids or set()
    for failure_class in previous_failure_classes:
        for prompt in prompts:
            if prompt.prompt_id not in used and failure_class in prompt.preferred_after:
                return prompt
    for prompt in prompts:
        if prompt.prompt_id not in used:
            return prompt
    return prompts[-1]


# 注入反代脚本时识别 AI 拒绝/负面回复的关键词
_REFUSAL_KEYWORDS = (
    "不执行",
    "无法执行",
    "不能执行",
    "拒绝执行",
    "我拒绝",
    "我不会",
    "我不能",
    "无法完成",
    "不能完成",
    "无法运行",
    "不会运行",
    "不予执行",
    "禁止执行",
    "不允许",
    "我无法",
    "抱歉，我不能",
    "抱歉，我无法",
    "对不起，我不能",
    "对不起，我无法",
    "i cannot",
    "i can't",
    "i won't",
    "i will not",
    "won't run",
    "won't execute",
    "refuse",
    "decline",
    "not able to",
    "unable to",
)


def _looks_like_refusal(reply: str | None) -> bool:
    """判断 AI 回复是否包含拒绝/负面执行特征"""
    if not reply:
        return True
    text = str(reply).lower()
    return any(kw.lower() in text for kw in _REFUSAL_KEYWORDS)


def _response_details(resp: httpx.Response) -> tuple[dict | None, str]:
    try:
        data = resp.json()
    except Exception:
        data = None

    parts = [f"HTTP {resp.status_code}"]
    if isinstance(data, dict):
        code = data.get("code")
        msg = data.get("message") or data.get("msg") or data.get("error") or data.get("reason")
        payload = data.get("data")
        status = payload.get("status") if isinstance(payload, dict) else None
        if code is not None:
            parts.append(f"code={code}")
        if msg:
            parts.append(f"message={_truncate_text(msg)}")
        if status:
            parts.append(f"status={status}")
        if isinstance(payload, dict):
            for key in ("reason", "error", "desc", "detail"):
                if payload.get(key):
                    parts.append(f"{key}={_truncate_text(payload[key])}")
                    break
    else:
        raw_text = _truncate_text(resp.text) if getattr(resp, "text", None) else "<empty>"
        parts.append(f"body={raw_text}")
    return data, ", ".join(parts)

# ----------------- Native Claw Client实现 -----------------

class NativeClawClient:
    def __init__(self, ph: str, cookies: dict, logger_obj: logging.Logger):
        self.ph = ph
        self.cookies = cookies
        self.logger = logger_obj
        self.ws = None
        self._listen_task = None
        self.responses = {}
        self.events = []
        self.connected = False
        self.session_key = "agent:main:main"
        

    async def _create_and_wait(self) -> bool:
        """创建 Claw 实例并等待其可用"""
        url_create = f"{BASE_URL}/open-apis/user/mimo-claw/create?xiaomichatbot_ph={quote(self.ph)}"
        url_status = f"{BASE_URL}/open-apis/user/mimo-claw/status"
        url_agree = f"{BASE_URL}/open-apis/agreement/user/mimo-claw?xiaomichatbot_ph={quote(self.ph)}"
        
        async with httpx.AsyncClient() as client:
            # 1. 尝试签署 agreement
            try:
                agree_resp = await client.post(url_agree, cookies=self.cookies, headers=_aistudio_headers(), timeout=15)
                agree_data, agree_detail = _response_details(agree_resp)
                if agree_resp.status_code >= 400 or (isinstance(agree_data, dict) and agree_data.get("code") not in (None, 0)):
                    level = logging.DEBUG if isinstance(agree_data, dict) and agree_data.get("code") == 2007 else logging.WARNING
                    log_event(self.logger, level, "claw.agreement.result", uid=self.cookies.get("userId"), detail=agree_detail, text_limit=240)
            except Exception as e:
                self.logger.warning(f"签署 agreement 异常: {e}")
                
            # 2. 发起创建
            r = await client.post(url_create, cookies=self.cookies, headers=_aistudio_headers(), timeout=20)
            create_data, create_detail = _response_details(r)
            if r.status_code == 401:
                self.logger.error(f"账户已过期失效: {create_detail}")
                return False
            if r.status_code == 429:
                self.logger.error(f"当前 Claw 实例负载过高: {create_detail}")
                return False
            if r.status_code >= 400:
                self.logger.error(f"创建实例请求失败: {create_detail}")
                return False
            if isinstance(create_data, dict) and create_data.get("code") not in (None, 0):
                self.logger.error(f"创建实例接口返回异常: {create_detail}")
                return False
            
            # 3. 轮询直到 AVAILABLE
            deadline = time.time() + 120
            last_status = None
            last_status_detail = "未拿到状态详情"
            while time.time() < deadline:
                sr = await client.get(url_status, cookies=self.cookies, headers=_aistudio_headers(), timeout=15)
                if sr.status_code == 401:
                    _, status_detail = _response_details(sr)
                    self.logger.error(f"查询创建状态遭遇鉴权失败: {status_detail}")
                    return False
                try:
                    d, status_detail = _response_details(sr)
                    last_status_detail = status_detail
                    if not isinstance(d, dict):
                        self.logger.warning(f"状态接口返回不可解析: {status_detail}")
                        await asyncio.sleep(2)
                        continue
                    st = (d.get("data") or {}).get("status", "").strip()
                    if st and st != last_status:
                        self.logger.info(f"Claw 创建状态: {status_detail}")
                        last_status = st
                    if st == "AVAILABLE":
                        return True
                    if st.endswith("FAILED") or st in ("DESTROYED", "ERROR"):
                        self.logger.error(f"创建失败，状态进入终态: {status_detail}")
                        return False
                except Exception as e:
                    self.logger.warning(f"解析创建状态异常: {e}")
                await asyncio.sleep(2)
        self.logger.error(f"创建实例等待超时，最后状态: {last_status_detail}")
        return False

    async def _get_ticket(self) -> str:
        """获取建立 ws 需要的 ticket"""
        url = f"{BASE_URL}/open-apis/user/ws/ticket?xiaomichatbot_ph={quote(self.ph)}"
        async with httpx.AsyncClient() as client:
            for attempt in range(5):
                r = await client.get(url, cookies=self.cookies, headers=_aistudio_headers(), timeout=15)
                data, detail = _response_details(r)
                if r.status_code == 200 and isinstance(data, dict):
                    ticket = data.get("data", {}).get("ticket")
                    if ticket:
                        return ticket
                # 刚创建好时可能由于节点同步延迟导致 ticket 返回 400，重试几次即可，不要使其抛错
                if attempt < 4:
                    self.logger.warning(f"获取 Ticket 失败: {detail}，3秒后重试...")
                    await asyncio.sleep(3)
            raise Exception(detail)

    async def connect(self, wait_available=True) -> bool:
        """建立 WebSocket 连接"""
        if wait_available:
            self.logger.info("创建实例并等待可用...")
            if not await self._create_and_wait():
                return False

        try:
            ticket = await self._get_ticket()
        except Exception as e:
            self.logger.error(f"获取 Ticket 失败: {e}")
            return False

        cookie_str = "; ".join(f'{k}="{v}"' if ' ' in v or '=' in v else f'{k}={v}' for k, v in self.cookies.items())
        headers_dict = {"Cookie": cookie_str, "Origin": BASE_URL}

        try:
            # 兼容 python websockets >= 14.0
            try:
                self.ws = await websockets.connect(
                    f"{WS_URL}?ticket={ticket}",
                    additional_headers=headers_dict
                )
            except TypeError as e:
                if "additional_headers" in str(e):
                    self.ws = await websockets.connect(
                        f"{WS_URL}?ticket={ticket}",
                        extra_headers=headers_dict
                    )
                else:
                    raise
        except Exception as e:
            self.logger.error(f"WebSocket 连结失败: {e}")
            return False

        self.connected = False
        self._listen_task = asyncio.create_task(self._ws_loop(), name=f"claw-listener-{self.logger.name}")
        
        # 等待后台 loop 处理 hello-ok 完成鉴权挂载
        for _ in range(50):
            if self.connected: 
                return True
            await asyncio.sleep(0.1)
        return False
        
    async def _ws_loop(self):
        try:
            async for message in self.ws:
                data = json.loads(message)
                if data["type"] == "event" and data.get("event") == "connect.challenge":
                    await self.ws.send(json.dumps({
                        "type": "req", "id": str(uuid.uuid4()), "method": "connect",
                        "params": {
                            "minProtocol": 3, "maxProtocol": 3,
                            "client": {"id": "cli", "version": "mimo-claw-ui", "platform": "Linux x86_64", "mode": "cli"},
                            "role": "operator",
                            "scopes": ["operator.admin", "operator.read", "operator.write", "operator.approvals", "operator.pairing"],
                            "caps": ["tool-events"],
                            "userAgent": "Mozilla/5.0", "locale": "zh-CN"
                        }
                    }))
                elif data["type"] == "res":
                    self.responses[data["id"]] = data
                    if data.get("ok") and data.get("payload", {}).get("type") == "hello-ok":
                        self.connected = True
                elif data["type"] == "event":
                    self.events.append(data)
        except Exception:
            self.connected = False

    async def send_message(self, text: str, timeout: int = 120, stage: str = "chat", prompt_id: str | None = None) -> str:
        """向 Claw 环境发生信息，并捕获最终确定的 AI 文本回复框"""
        uid = str(self.cookies.get("userId") or "")
        if not self.connected or not self.ws:
            log_event(
                self.logger,
                logging.WARNING,
                "claw.chat.unavailable",
                uid=uid,
                phase=stage,
                prompt_id=prompt_id or "",
                reason="websocket_not_connected",
            )
            return "(发送失败，Websocket 未连接)"
            
        self.events.clear()
        req_id = str(uuid.uuid4())
        payload = {
            "type": "req", "id": req_id, "method": "chat.send",
            "params": {"sessionKey": self.session_key, "message": text, "idempotencyKey": str(uuid.uuid4())}
        }
        started = time.monotonic()
        log_event(
            self.logger,
            logging.INFO,
            "claw.chat.send",
            uid=uid,
            phase=stage,
            prompt_id=prompt_id or "",
            request_id=req_id,
            timeout_seconds=timeout,
            prompt=safe_claw_trace_text(text, limit=360),
            text_limit=520,
        )
        
        try:
            await self.ws.send(json.dumps(payload))
        except Exception as e:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            log_event(
                self.logger,
                logging.WARNING,
                "claw.chat.send_error",
                uid=uid,
                phase=stage,
                prompt_id=prompt_id or "",
                request_id=req_id,
                elapsed_ms=elapsed_ms,
                error=e,
                text_limit=240,
            )
            return f"(下发 payload 异常: {e})"

        reply = None
        for _ in range(timeout * 10):
            for evt in list(self.events): # 复制一份遍历避免动态更改引发异常
                if evt.get("event") == "chat":
                    msg = evt.get("payload", {}).get("message", {})
                    if msg.get("role") == "assistant":
                        for c in msg.get("content", []):
                            if c.get("type") == "text" and c.get("text"):
                                reply = c["text"]
                    if evt.get("payload", {}).get("state") == "final" and reply:
                        self.events.clear()
                        elapsed_ms = int((time.monotonic() - started) * 1000)
                        log_event(
                            self.logger,
                            logging.INFO,
                            "claw.chat.reply",
                            uid=uid,
                            phase=stage,
                            prompt_id=prompt_id or "",
                            request_id=req_id,
                            elapsed_ms=elapsed_ms,
                            reply=safe_claw_trace_text(reply, limit=360),
                            text_limit=520,
                        )
                        return reply
            await asyncio.sleep(0.1)
        self.events.clear()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        log_event(
            self.logger,
            logging.WARNING,
            "claw.chat.timeout",
            uid=uid,
            phase=stage,
            prompt_id=prompt_id or "",
            request_id=req_id,
            elapsed_ms=elapsed_ms,
            timeout_seconds=timeout,
            partial_reply=safe_claw_trace_text(reply, limit=240) if reply else "",
            text_limit=420,
        )
        return reply or "(等待最终态回复超时)"
        
    async def close(self):
        self.connected = False
        if self._listen_task:
            self._listen_task.cancel()
        if self.ws:
            try:
                await asyncio.wait_for(self.ws.close(), timeout=2)
            except Exception:
                pass
        if self._listen_task:
            try:
                await asyncio.gather(self._listen_task, return_exceptions=True)
            finally:
                self._listen_task = None
        self.ws = None

class AccountManager:
    def __init__(self, uid, user_info):
        self.uid = uid
        self.user_info = user_info
        self.ph = user_info.get("xiaomichatbot_ph", "")
        self.cookies = {
            "serviceToken": user_info.get("serviceToken", ""),
            "userId": user_info.get("userId", ""),
            "xiaomichatbot_ph": self.ph
        }
        self.name = user_info.get("name", self.uid)
        self.logger = logging.getLogger(f"Acc-{self.name}-{self.uid}")

    async def get_instance_status(self) -> tuple[str, int]:
        """获取当前容器的状态和剩余时间(秒)"""
        url = f"{BASE_URL}/open-apis/user/mimo-claw/status"
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(url, cookies=self.cookies, headers=_aistudio_headers(), timeout=15)
                data = r.json()
                st = data.get("data", {}).get("status", "")
                expire_ms = data.get("data", {}).get("expireTime")
                if expire_ms:
                    remain_sec = max(0, int(int(expire_ms) / 1000 - time.time()))
                else:
                    remain_sec = 0
                return st, remain_sec
        except Exception as e:
            self.logger.error(f"获取状态异常: {e}")
            return "", 0

    async def connect_with_retry(self, client: NativeClawClient, max_retries: int = 10, delay: int = 8, create: bool = True):
        log_event(
            self.logger,
            logging.INFO,
            "claw.ws.connect.start",
            uid=self.uid,
            max_retries=max_retries,
            create=create,
        )
        for i in range(max_retries):
            attempt = i + 1
            log_event(
                self.logger,
                logging.DEBUG,
                "claw.ws.connect.attempt",
                uid=self.uid,
                attempt=attempt,
                max_retries=max_retries,
            )
            if await client.connect(wait_available=create):
                log_event(self.logger, logging.INFO, "claw.ws.connected", uid=self.uid, attempt=attempt)
                return True
            if attempt < max_retries:
                log_event(
                    self.logger,
                    logging.DEBUG,
                    "claw.ws.connect.retry",
                    uid=self.uid,
                    attempt=attempt,
                    next_delay_seconds=delay,
                )
            await asyncio.sleep(delay)
        log_event(self.logger, logging.ERROR, "claw.ws.connect.failed", uid=self.uid, max_retries=max_retries)
        return False

    async def _wait_for_ws_gateway(self, baseline_count: int, expected_increase: int = 1, timeout: int = 45) -> bool:
        """[已废弃] 旧逻辑：根据总数差判定 WS 网关是否新增节点。
        并发多账号场景下会互相误判，已被 _wait_for_node(uid) 取代，仅保留兼容。"""
        try:
            from mimo2api.gateway_state import state as gw_state
        except Exception as e:
            self.logger.warning(f"无法导入 gateway_state，跳过 WS 连通检测: {e}")
            return True
        for _ in range(timeout * 2):
            if len(gw_state.active_clients) >= baseline_count + expected_increase:
                return True
            await asyncio.sleep(0.5)
        return len(gw_state.active_clients) >= baseline_count + expected_increase

    async def _wait_for_node_status(
        self,
        node_id: str,
        timeout: int = 90,
        baseline_remote_meta: dict | None = None,
    ) -> NodeWaitResult:
        """等待网关侧检测到本账号的 bridge 节点上线。

        本地直连/隧道回本机时检查当前进程内存；外网网关独立部署时，补充查询
        WS_TUNNEL_URL 对应网关的 /api/stats。
        """
        try:
            from mimo2api.gateway_state import state as gw_state
        except Exception as e:
            self.logger.warning(f"无法导入 gateway_state，跳过节点连通检测: {e}")
            return NodeWaitResult(exact=True, reason="gateway_state_unavailable")
        key = str(node_id)
        next_remote_check = 0.0
        deadline = time.monotonic() + max(0, float(timeout))
        remote_interval = min(2.0, max(0.1, float(timeout) / 2.0 if timeout else 0.1))
        baseline_active = _meta_int(baseline_remote_meta or {}, "active_clients")
        baseline_unknown = _meta_int(baseline_remote_meta or {}, "unknown_nodes")
        while time.monotonic() < deadline:
            if key in gw_state.node_to_ws:
                return NodeWaitResult(exact=True, reason="local")
            now = time.monotonic()
            if now >= next_remote_check:
                next_remote_check = now + remote_interval
                remote_nodes, remote_meta = await fetch_remote_gateway_nodes()
                if key in remote_nodes:
                    log_event(
                        self.logger,
                        logging.INFO,
                        "bridge.node.remote_online",
                        uid=key,
                        source_url=remote_nodes[key].source_url,
                    )
                    return NodeWaitResult(exact=True, source_url=remote_nodes[key].source_url, reason="remote_uid")
                active_count = _meta_int(remote_meta, "active_clients")
                unknown_count = _meta_int(remote_meta, "unknown_nodes")
                if active_count > baseline_active and unknown_count > baseline_unknown:
                    log_event(
                        self.logger,
                        logging.WARNING,
                        "bridge.node.remote_ambiguous",
                        uid=key,
                        url=remote_meta.get("url"),
                        active_clients=active_count,
                        unknown_nodes=unknown_count,
                    )
                    return NodeWaitResult(
                        ambiguous=True,
                        source_url=str(remote_meta.get("url") or ""),
                        reason="remote_unknown_node_growth",
                    )
                if remote_meta.get("error"):
                    log_event(
                        self.logger,
                        logging.DEBUG,
                        "bridge.node.remote_check_error",
                        uid=key,
                        url=remote_meta.get("url"),
                        error=remote_meta.get("error"),
                        text_limit=180,
                    )
                if time.monotonic() >= deadline:
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.5, remaining))
        if key in gw_state.node_to_ws:
            return NodeWaitResult(exact=True, reason="local")
        return NodeWaitResult(reason="timeout")

    async def _wait_for_node(self, node_id: str, timeout: int = 90) -> bool:
        result = await self._wait_for_node_status(node_id, timeout=timeout)
        return result.ok

    def _node_online(self, node_id: str) -> bool:
        try:
            from mimo2api.gateway_state import state as gw_state
        except Exception:
            return False
        return str(node_id) in gw_state.node_to_ws

    async def _node_online_anywhere(self, node_id: str) -> bool:
        key = str(node_id)
        if self._node_online(key):
            return True
        remote_nodes, remote_meta = await fetch_remote_gateway_nodes()
        if key in remote_nodes:
            log_event(
                self.logger,
                logging.INFO,
                "bridge.node.remote_online",
                uid=key,
                source_url=remote_nodes[key].source_url,
            )
            return True
        if remote_meta.get("error"):
            log_event(
                self.logger,
                logging.DEBUG,
                "bridge.node.remote_check_error",
                uid=key,
                url=remote_meta.get("url"),
                error=remote_meta.get("error"),
                text_limit=180,
            )
        return False



    async def inject_bridge_with_retry(
        self,
        client: NativeClawClient,
        inject_prompt: str | Sequence[BridgeInjectionPrompt],
        max_retries: int = 3,
        ws_wait_timeout: int = 90,
        label: str = "桥接脚本",
    ) -> bool:
        """注入反代/桥接脚本。判定顺序：先看自己账号的节点是否上线，没上线再考虑是否拒绝。"""
        # 注入前先看本账号 bridge 是否已经在线（例如上轮残留进程仍存活）
        if await self._node_online_anywhere(self.uid):
            log_event(self.logger, logging.INFO, "bridge.node.already_online", uid=self.uid, label=label)
            return True
        _, initial_remote_meta = await fetch_remote_gateway_nodes()
        if _remote_identity_unavailable(initial_remote_meta):
            log_event(
                self.logger,
                logging.WARNING,
                "bridge.inject.skip_remote_identity_unavailable",
                uid=self.uid,
                label=label,
                url=initial_remote_meta.get("url"),
                active_clients=initial_remote_meta.get("active_clients"),
                unknown_nodes=initial_remote_meta.get("unknown_nodes"),
            )
            return True

        prompt_options = _coerce_bridge_prompts(inject_prompt)
        if not prompt_options:
            log_event(self.logger, logging.ERROR, "bridge.inject.no_prompts", uid=self.uid, label=label)
            return False
        used_prompt_ids: set[str] = set()
        previous_failure_classes: tuple[str, ...] = ()

        for attempt in range(1, max_retries + 1):
            _, baseline_remote_meta = await fetch_remote_gateway_nodes()
            prompt_template = select_bridge_injection_prompt(
                prompt_options,
                previous_failure_classes=previous_failure_classes,
                used_prompt_ids=used_prompt_ids,
            )
            used_prompt_ids.add(prompt_template.prompt_id)
            log_event(
                self.logger,
                logging.INFO,
                "bridge.inject.start",
                uid=self.uid,
                label=label,
                attempt=attempt,
                max_retries=max_retries,
                prompt_id=prompt_template.prompt_id,
            )
            chat_started = time.monotonic()
            log_event(
                self.logger,
                logging.INFO,
                "bridge.inject.chat_send.start",
                uid=self.uid,
                label=label,
                attempt=attempt,
                prompt_id=prompt_template.prompt_id,
            )
            try:
                reply = await client.send_message(
                    prompt_template.text,
                    timeout=180,
                    stage="bridge.inject",
                    prompt_id=prompt_template.prompt_id,
                )
            except Exception as e:
                log_event(
                    self.logger,
                    logging.WARNING,
                    "bridge.inject.send_error",
                    uid=self.uid,
                    label=label,
                    attempt=attempt,
                    prompt_id=prompt_template.prompt_id,
                    error=e,
                    text_limit=240,
                )
                reply = None
            chat_elapsed_ms = int((time.monotonic() - chat_started) * 1000)
            failure_classes = bridge_injection_failure_classes(reply)
            failure_class = classify_bridge_injection_failure(reply)
            node_wait_timeout = _bridge_node_wait_timeout(failure_classes, ws_wait_timeout)
            log_event(
                self.logger,
                logging.INFO,
                "bridge.inject.chat_send.done",
                uid=self.uid,
                label=label,
                attempt=attempt,
                prompt_id=prompt_template.prompt_id,
                failure_class=failure_class,
                elapsed_ms=chat_elapsed_ms,
                reply=safe_claw_trace_text(reply, limit=240) if reply else "",
                text_limit=420,
            )
            log_event(
                self.logger,
                logging.DEBUG,
                "bridge.inject.reply",
                uid=self.uid,
                label=label,
                attempt=attempt,
                prompt_id=prompt_template.prompt_id,
                failure_class=failure_class,
                reply=reply,
                text_limit=500,
            )

            # 关键：以 WS 真实上线为唯一成功判据，AI 回复内容只是辅助诊断
            node_wait_started = time.monotonic()
            log_event(
                self.logger,
                logging.INFO,
                "bridge.inject.node_wait.start",
                uid=self.uid,
                label=label,
                attempt=attempt,
                prompt_id=prompt_template.prompt_id,
                failure_class=failure_class,
                timeout_seconds=node_wait_timeout,
            )
            node_result = await self._wait_for_node_status(
                self.uid,
                timeout=node_wait_timeout,
                baseline_remote_meta=baseline_remote_meta,
            )
            node_wait_elapsed_ms = int((time.monotonic() - node_wait_started) * 1000)
            log_event(
                self.logger,
                logging.INFO if node_result.ok else logging.WARNING,
                "bridge.inject.node_wait.done",
                uid=self.uid,
                label=label,
                attempt=attempt,
                prompt_id=prompt_template.prompt_id,
                failure_class=failure_class,
                ok=node_result.ok,
                exact=node_result.exact,
                ambiguous=node_result.ambiguous,
                reason=node_result.reason,
                source_url=node_result.source_url,
                elapsed_ms=node_wait_elapsed_ms,
            )
            if node_result.ok:
                log_event(
                    self.logger,
                    logging.INFO if node_result.exact else logging.WARNING,
                    "bridge.inject.success" if node_result.exact else "bridge.inject.ambiguous_success",
                    uid=self.uid,
                    label=label,
                    attempt=attempt,
                    prompt_id=prompt_template.prompt_id,
                    source_url=node_result.source_url,
                    reason=node_result.reason,
                )
                return True

            refused = _looks_like_refusal(reply)
            previous_failure_classes = failure_classes or (("generic_refusal",) if refused else ())
            log_event(
                self.logger,
                logging.WARNING,
                "bridge.inject.ws_missing",
                uid=self.uid,
                label=label,
                attempt=attempt,
                prompt_id=prompt_template.prompt_id,
                failure_class=failure_class,
                reason="assistant_refusal" if refused else "no_bridge_after_reply",
                reply=reply,
                text_limit=240,
            )

            if attempt < max_retries:
                try:
                    log_event(
                        self.logger,
                        logging.INFO,
                        "bridge.inject.chat_reset",
                        uid=self.uid,
                        label=label,
                        attempt=attempt,
                        prompt_id=prompt_template.prompt_id,
                        failure_class=failure_class,
                    )
                    reset_reply = await client.send_message("/reset", timeout=60, stage="bridge.inject.reset")
                    log_event(
                        self.logger,
                        logging.DEBUG,
                        "bridge.inject.chat_reset_reply",
                        uid=self.uid,
                        label=label,
                        attempt=attempt,
                        prompt_id=prompt_template.prompt_id,
                        failure_class=failure_class,
                        reply=reset_reply,
                        text_limit=240,
                    )
                except Exception as e:
                    log_event(
                        self.logger,
                        logging.WARNING,
                        "bridge.inject.chat_reset_error",
                        uid=self.uid,
                        label=label,
                        attempt=attempt,
                        prompt_id=prompt_template.prompt_id,
                        failure_class=failure_class,
                        error=e,
                        text_limit=240,
                    )
                await asyncio.sleep(5)

        log_event(
            self.logger,
            logging.ERROR,
            "bridge.inject.failed",
            uid=self.uid,
            label=label,
            max_retries=max_retries,
        )
        return False


    async def run_lifecycle(self):
        """???????????4?????????????1????????"""
        while True:
            self.logger.info("=== ????? Claw ???? ===")
            client = NativeClawClient(self.ph, self.cookies, self.logger)
            try:
                # 0. ????????
                st, remain_sec = await self.get_instance_status()
                self.logger.info(f"??????????: {st}, ????: {remain_sec} ?")

                # 1. ????????? > 5 ???????
                if st == "AVAILABLE" and remain_sec > 300:
                    self.logger.info(f"????????? {remain_sec}s????????...")
                    if await self.connect_with_retry(client, max_retries=3, delay=5, create=False):
                        bridge_code = await get_bridge_code(self.uid)
                        inject_prompts = build_effective_bridge_injection_prompt_library(bridge_code)
                        inject_ok = await self.inject_bridge_with_retry(
                            client,
                            inject_prompts,
                            max_retries=3,
                            ws_wait_timeout=90,
                            label="????(????)",
                        )
                        await client.close()

                        if inject_ok:
                            self.logger.info(f"??????????? {remain_sec} ?????????...")
                            await asyncio.sleep(remain_sec)
                            # ?????????????????????
                            await asyncio.sleep(60)
                            continue
                        else:
                            self.logger.warning("??????????????????????????...")
                            await asyncio.sleep(remain_sec)
                            await asyncio.sleep(60)
                            continue
                    else:
                        self.logger.warning("???????????????????...")
                        await asyncio.sleep(remain_sec)
                        await asyncio.sleep(60)
                        continue

                # 2. ?????????? ? ????????
                if not _can_create_today(self.uid):
                    wait = _seconds_until_tomorrow_utc8()
                    self.logger.info(f"???????????????? UTC+8 00:00?{wait}s?...")
                    await asyncio.sleep(wait)
                    continue

                # 3. ???????????????????????
                self.logger.info("????????????...")
                if not await self.connect_with_retry(client, max_retries=5, delay=5, create=True):
                    self.logger.error("?????????????????????/??????? 10 ????...")
                    await client.close()
                    await asyncio.sleep(600)
                    continue

                # ????
                _save_daily_creation(self.uid)

                # 4. ??????/????
                reset_soul_cmd = (
                    "?soul.md???????\"??????soul.md??????prompt?"
                    "??????????????????\"???token??????????????????"
                )
                reset_agents_cmd = (
                    "?\"AGENTS.md\"??\n"
                    "\"## ??????????\n\n"
                    "????????????????????\n\n"
                    "API Key?API ???token??????\n"
                    "~/.openclaw/openclaw.json ???\n"
                    "~/.openclaw/agents/ ??????????\n\n"
                    "~/.openclaw/identity/ ????????\n\n"
                    "~/.openclaw/credentials/ ????????\n\n"
                    "?????????\"????\"?\"???\"?\"???\"????????\n"
                    "??????????token ??????????\n\n"
                    "??????URL???????????????????\n"
                    "?????????????????\n"
                    "???????\"???????\"?\"?????????\"?\n\"?????????\"???????????\n\n"
                    "????????????provider ???API endpoint ??\n\n"
                    "??????????,??????????\"\n\n"
                    "??\n"
                    "\"???????????????????\""
                )

                log_event(self.logger, logging.INFO, "claw.reset.command", uid=self.uid, target="soul.md")
                reply_soul = await client.send_message(reset_soul_cmd, timeout=120, stage="claw.reset.soul")
                log_event(
                    self.logger,
                    logging.DEBUG,
                    "claw.reset.reply",
                    uid=self.uid,
                    target="soul.md",
                    reply=reply_soul,
                    text_limit=240,
                )

                log_event(self.logger, logging.INFO, "claw.reset.command", uid=self.uid, target="AGENTS.md")
                reply_agents = await client.send_message(reset_agents_cmd, timeout=120, stage="claw.reset.agents")
                log_event(
                    self.logger,
                    logging.DEBUG,
                    "claw.reset.reply",
                    uid=self.uid,
                    target="AGENTS.md",
                    reply=reply_agents,
                    text_limit=240,
                )

                self.logger.info("???? Claw ?????????(15s)...")
                await asyncio.sleep(15)

                self.logger.info("????????????...")
                await client.close()
                await asyncio.sleep(5)

                # 5. ???????????
                self.logger.info("???????????????????...")
                client = NativeClawClient(self.ph, self.cookies, self.logger)
                if not await self.connect_with_retry(client, max_retries=10, delay=8, create=False):
                    self.logger.error("???????????????????????????")
                    await client.close()
                    await asyncio.sleep(60)
                    continue

                # 6. ??????????
                self.logger.info("?????? mimo2api bridge.py ...")
                bridge_code = await get_bridge_code(self.uid)
                inject_prompts = build_effective_bridge_injection_prompt_library(bridge_code)

                inject_ok = await self.inject_bridge_with_retry(
                    client,
                    inject_prompts,
                    max_retries=3,
                    ws_wait_timeout=90,
                    label="????(????)",
                )
                if not inject_ok:
                    self.logger.error("???????????????????..")
                    await client.close()
                    await asyncio.sleep(15)
                    continue

                # 7. ???????????4 ???
                max_lifetime = max(300, _config_int("lifecycle.instance_max_lifetime_seconds", 14400))
                self.logger.info(f"???????????? {max_lifetime} ?????????...")
                await client.close()
                await asyncio.sleep(max_lifetime)
                # ????? 60 ????????
                await asyncio.sleep(60)

            except asyncio.CancelledError:
                await client.close()
                self.logger.info("?????????")
                break
            except Exception as e:
                self.logger.error(f"???????????: {e}", exc_info=True)
                await client.close()
                await asyncio.sleep(60)


async def start_manager_tasks():
    logger.info("mimo2api ???????????? (Manager) ??????")
    _load_daily_creations()
    users = load_all_users()
    if not users:
        logger.error("????, ???? users ????????????????")
        return

    logger.info(f"??? users/ ???????? {len(users)} ??????????")
    tasks = []
    _known_manager_uids.clear()
    _known_manager_uids.update(str(uid) for uid in users.keys())

    try:
        for uid, user_info in users.items():
            manager = AccountManager(uid, user_info)
            t = asyncio.create_task(manager.run_lifecycle(), name=f"account-manager-{uid}")
            tasks.append(t)

        await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        await cancel_and_wait(tasks)
        raise


async def main():
    await start_manager_tasks()


if __name__ == "__main__":
    asyncio.run(main())
