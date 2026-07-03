"""
claw_deployer — 单账号全流程部署器（Step 2 核心）。

把 test_inject.py 的 ad-hoc 流程封装成可复用、可重试、错误可分类的 ClawDeployer。
5 阶段：load_prompt → ensure_instance → ws_handshake → send_inject → verify。
每阶段独立重试预算；deploy_refused 时从 prompt_store 换模板限量重试。
L3（Cloudflare API connector 配对）可选——tunnel_health=None 时跳过，靠 L1+L2。

返回结构化 DeployResult，供 Step 3 调度器决策（success / needs_relogin / error_type）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from claw_client import BASE_URL, NativeClawClient, _aistudio_headers, safe_claw_trace_text
from deploy_errors import (
    AUTH_EXPIRED, CREATE_FAILED, CREATE_RATE_LIMITED, DEPLOY_REFUSED,
    NETWORK_ERROR, SEND_TIMEOUT, SUCCESS, VERIFY_FAILED, WS_CONNECT_FAILED,
    WS_DISCONNECTED,
    classify_instance_status, classify_reply, classify_ws_error,
    extract_connector_id, is_retryable, is_terminal, needs_relogin,
)
from prompt_store import PromptStore

try:
    import websockets
except Exception:  # pragma: no cover
    websockets = None  # claw_client 已依赖，正常应有


# ----------------- DeployResult -----------------

@dataclass
class DeployResult:
    success: bool
    uid: str
    error_type: Optional[str] = None
    error_detail: Optional[str] = None
    needs_relogin: bool = False
    reply: Optional[str] = None
    prompt_id_used: Optional[str] = None
    conversation_log: Optional[Path] = None
    instance_status: str = ""
    instance_remain_sec: int = 0
    connector_id: Optional[str] = None
    elapsed_sec: float = 0.0
    attempts: dict[str, int] = field(default_factory=dict)


# ----------------- Credentials + utils -----------------

def load_credentials(path: Path) -> dict:
    """加载凭据，剥离值前后的多余引号（注册机产物带 \"...\" 包裹）。"""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    out = {}
    for k, v in raw.items():
        s = str(v) if v is not None else ""
        if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
            s = s[1:-1]
        out[k] = s
    return out


def build_logger(name: str = "claw-deployer") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # Windows GBK 防 emoji 崩
        except Exception:
            pass
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(h)
    return logger


def credentials_to_client_params(creds: dict) -> tuple[str, dict, str]:
    """从加载的凭据 dict 抽出 (ph, cookies, uid)。"""
    uid = creds.get("userId", "")
    ph = creds.get("xiaomichatbot_ph", "")
    cookies = {
        "serviceToken": creds.get("serviceToken", ""),
        "userId": uid,
        "xiaomichatbot_ph": ph,
    }
    return ph, cookies, uid


# ----------------- DeployError -----------------

class DeployError(Exception):
    def __init__(self, error_type: str, detail: str = ""):
        self.error_type = error_type
        self.detail = detail
        super().__init__(f"{error_type}: {detail}")


_SECRET_RECORD_KEYS = {
    "authorization",
    "cookie",
    "api_key",
    "mimo_api_key",
    "mimo_api_endpoint",
    "tunnel_token",
    "proxy_api_key",
    "cf_api_token",
    "cf_account_id",
    "servicetoken",
    "xiaomichatbot_ph",
    "session_secret",
    "webui_session",
}


def _is_secret_record_key(key: object) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return normalized in _SECRET_RECORD_KEYS


def _redact_recorded_value(value):
    """递归脱敏 WS 录制内容，保留结构但不落盘密钥。"""
    if isinstance(value, str):
        return safe_claw_trace_text(value, limit=max(len(value), 360))
    if isinstance(value, dict):
        return {
            k: "<redacted>" if _is_secret_record_key(k) else _redact_recorded_value(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_recorded_value(v) for v in value]
    return value


# ----------------- RecordingClawClient -----------------

class RecordingClawClient(NativeClawClient):
    """在 _ws_loop 之外录制每条 WS 进出消息到 JSONL。从 test_inject.py 迁移。"""

    def __init__(self, *args, record_path: Path, **kwargs):
        super().__init__(*args, **kwargs)
        self._record_path = record_path
        self._record_file = None
        self._msg_seq = 0

    def _open_record(self):
        self._record_file = open(self._record_path, "a", encoding="utf-8")

    def _record(self, direction: str, data):
        self._msg_seq += 1
        if self._record_file:
            line = {"seq": self._msg_seq, "ts": time.time(), "dir": direction, "data": _redact_recorded_value(data)}
            self._record_file.write(json.dumps(line, ensure_ascii=False) + "\n")
            self._record_file.flush()

    async def _ws_loop(self):
        try:
            async for message in self.ws:
                try:
                    data = json.loads(message)
                except Exception:
                    data = {"_raw": message}
                self._record("in", data)
                if isinstance(data, dict):
                    if data.get("type") == "event" and data.get("event") == "connect.challenge":
                        handshake = {
                            "type": "req", "id": str(uuid.uuid4()), "method": "connect",
                            "params": {
                                "minProtocol": 4, "maxProtocol": 4,
                                "client": {"id": "cli", "version": "mimo-claw-ui",
                                           "platform": "Linux x86_64", "mode": "cli"},
                                "role": "operator",
                                "scopes": ["operator.admin","operator.read","operator.write",
                                           "operator.approvals","operator.pairing"],
                                "caps": ["tool-events"], "userAgent": "Mozilla/5.0", "locale": "zh-CN",
                            },
                        }
                        await self.ws.send(json.dumps(handshake))
                        self._record("out", handshake)
                    elif data.get("type") == "res":
                        self.responses[data["id"]] = data
                        if data.get("ok") and data.get("payload", {}).get("type") == "hello-ok":
                            self.connected = True
                    elif data.get("type") == "event":
                        self.events.append(data)
        except Exception:
            self.connected = False

    async def connect(self, wait_available: bool = True) -> bool:
        self._open_record()
        ok = await super().connect(wait_available=wait_available)
        if ok and self.ws is not None:
            orig_send = self.ws.send
            async def _logging_send(payload, *a, **kw):
                try:
                    parsed = json.loads(payload) if isinstance(payload, str) else payload
                except Exception:
                    parsed = {"_raw": str(payload)}
                self._record("out", parsed)
                return await orig_send(payload, *a, **kw)
            self.ws.send = _logging_send
        return ok

    async def close(self):
        await super().close()
        if self._record_file:
            try:
                self._record_file.close()
            except Exception:
                pass
            self._record_file = None


# ----------------- ClawDeployer -----------------

class ClawDeployer:
    """单账号一次全流程部署。Step 3 调度器的原子单元。"""

    def __init__(
        self,
        credentials: dict,
        logger: logging.Logger,
        *,
        prompt_store: PromptStore,
        out_dir: Path,
        max_attempts_per_phase: int = 3,
        base_backoff: float = 2.0,
        send_timeout: int = 900,
        verify: bool = True,
        tunnel_health=None,            # Optional[TunnelHealth]；None→跳过 L3
        public_hostname: Optional[str] = None,  # L1 公网探测目标
        proxy_api_key: Optional[str] = None,    # L1 公网探测用的 bearer
    ):
        self._creds = credentials
        self._logger = logger
        self._prompt_store = prompt_store
        self._out_dir = Path(out_dir)
        self._out_dir.mkdir(parents=True, exist_ok=True)
        self._max_attempts = max_attempts_per_phase
        self._base_backoff = base_backoff
        self._send_timeout = send_timeout
        self._verify = verify
        self._tunnel_health = tunnel_health
        self._public_hostname = public_hostname
        self._proxy_api_key = proxy_api_key

        ph, cookies, uid = credentials_to_client_params(credentials)
        self._ph = ph
        self._cookies = cookies
        self._uid = uid
        self._log_path = self._out_dir / f"conversation_log_user_{uid or 'unknown'}.jsonl"
        # 清旧日志
        if self._log_path.exists():
            self._log_path.unlink()
        self.client = RecordingClawClient(
            ph=ph, cookies=cookies, logger_obj=logger, record_path=self._log_path
        )
        self._attempts: dict[str, int] = {}

    # ---- 通用重试包装 ----
    async def _retry(self, phase: str, coro_factory, *, retryable_override: Optional[set] = None):
        """跑 coro_factory() 最多 max_attempts 次。返回 (ok, value, DeployError|None)。
        仅对 is_retryable(error_type) 重试；终态或不可重试立即返回。"""
        last_err: Optional[DeployError] = None
        for i in range(self._max_attempts):
            self._attempts[phase] = i + 1
            try:
                value = await coro_factory()
                return True, value, None
            except DeployError as e:
                last_err = e
                et = e.error_type
                retryable = retryable_override if retryable_override is not None else {x for x in _ALL_RETRYABLE()}
                if is_terminal(et) or et not in retryable:
                    return False, None, e
                backoff = min(self._base_backoff * (2 ** i), 30.0)
                self._logger.info(f"[{phase}] 第{i+1}次失败 ({et})，{backoff:.1f}s 后重试")
                await asyncio.sleep(backoff)
        return False, None, last_err

    # ---- phase 1: ensure_instance ----
    async def _probe_status(self) -> tuple[str, int, int]:
        """自带 HTTP code 的状态探测。401 → auth_expired。"""
        url = f"{BASE_URL}/open-apis/user/mimo-claw/status"
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(url, cookies=self._cookies, headers=_aistudio_headers(), timeout=15)
        except Exception as e:
            raise DeployError(NETWORK_ERROR, f"status 探测网络异常: {e}")
        if r.status_code == 401:
            raise DeployError(AUTH_EXPIRED, "status 401：cookie 失效")
        try:
            data = r.json()
        except Exception:
            raise DeployError(NETWORK_ERROR, f"status 非 JSON: HTTP {r.status_code}")
        d = data.get("data") or {}
        st = str(d.get("status", "")).strip()
        expire_ms = d.get("expireTime")
        remain = max(0, int(int(expire_ms) / 1000 - time.time())) if expire_ms else 0
        return st, remain, r.status_code

    async def _ensure_instance(self) -> bool:
        """查状态决定 wait_available。401→auth_expired；终态失败→create_failed。"""
        st, remain, code = await self._probe_status()
        self._logger.info(f"[ensure] status={st!r} remain={remain}s http={code}")
        if st == "AVAILABLE":
            self._logger.info("[ensure] 实例已可用，跳过创建")
            return False  # wait_available=False
        et = classify_instance_status(st)
        if et == CREATE_FAILED:
            raise DeployError(CREATE_FAILED, f"实例终态: {st}")
        # CREATING / NOT_CREATED / 空 → 需要创建
        return True  # wait_available=True

    # ---- phase 2: ws_handshake ----
    async def _ws_handshake(self, wait_available: bool) -> bool:
        """connect() 做 create+ticket+WS。失败时回查 status 区分 auth_expired。"""
        try:
            ok = await self.client.connect(wait_available=wait_available)
        except Exception as e:
            et = classify_ws_error(e, phase="handshake")
            if et == AUTH_EXPIRED:
                raise DeployError(AUTH_EXPIRED, f"WS 握手鉴权失败: {e}")
            raise DeployError(et, f"WS 握手异常: {e}")
        if ok:
            return True
        # connect 返回 False——先看 last_create_error（create API 失败的精确原因，区分 7001 限流等）
        ce = getattr(self.client, "last_create_error", None)
        if isinstance(ce, dict):
            reason = ce.get("reason")
            api_code = ce.get("api_code")
            detail = ce.get("detail", "")
            if reason == "auth_expired":
                raise DeployError(AUTH_EXPIRED, f"create 401: {detail}")
            if reason == "rate_limited":  # HTTP 429
                raise DeployError(CREATE_RATE_LIMITED, f"create 429 限流: {detail}")
            if reason == "api_code_error":
                if api_code == 7001:
                    raise DeployError(CREATE_RATE_LIMITED, f"create code=7001 限流: {detail}")
                raise DeployError(CREATE_FAILED, f"create code={api_code}: {detail}")
            if reason in ("terminal_status", "timeout", "http_error"):
                raise DeployError(CREATE_FAILED, f"create {reason}: {detail}")
        # 无 create_error（wait_available=False 跳过 create，或 create 成功但 WS 握手挂）→ 回查 status 判 401
        try:
            st2, _, code2 = await self._probe_status()
        except DeployError:
            raise
        except Exception as e:
            raise DeployError(WS_CONNECT_FAILED, f"connect 失败且回查 status 异常: {e}")
        if code2 == 401:
            raise DeployError(AUTH_EXPIRED, "connect 失败 + status 401：cookie 失效")
        raise DeployError(WS_CONNECT_FAILED, f"connect 失败，status={st2!r} http={code2}")

    # ---- phase 3: send_inject ----
    async def _send_inject(self, prompt_text: str) -> Optional[str]:
        """send_message 拿回复。None/空 → send_timeout。"""
        try:
            reply = await self.client.send_message(
                prompt_text, timeout=self._send_timeout,
                stage="skill.inject", prompt_id="deploy",
            )
        except Exception as e:
            et = classify_ws_error(e, phase="send")
            raise DeployError(et, f"send_message 异常: {e}")
        if not reply:
            raise DeployError(SEND_TIMEOUT, "send_message 返回空（无 final 回复）")
        if "等待最终态回复超时" in reply:
            raise DeployError(SEND_TIMEOUT, reply)
        if "Websocket 未连接" in reply or "WebSocket 未连接" in reply:
            raise DeployError(WS_DISCONNECTED, reply)
        if "下发 payload 异常" in reply:
            raise DeployError(NETWORK_ERROR, reply)
        return reply

    # ---- phase 4: verify ----
    async def _verify_reply(self, reply: str) -> tuple[bool, Optional[str], Optional[str]]:
        """返回 (success, connector_id, error_type)。
        L1: classify_reply；L2: extract_connector_id；L3: tunnel_health.is_replica_active（可选）。"""
        et = classify_reply(reply)
        if et != SUCCESS:
            return False, None, et
        cid = extract_connector_id(reply)
        # L3（可选）：connector_id 在 Cloudflare 活跃列表里
        if self._tunnel_health is not None:
            if not cid:
                return False, None, VERIFY_FAILED
            try:
                active = await self._tunnel_health.is_replica_active(cid)
            except Exception as e:
                self._logger.warning(f"[verify] L3 异常降级: {e}")
                active = True  # API 异常不阻塞，靠 L1+L2
            if not active:
                return False, cid, VERIFY_FAILED  # cloudflared 跑了但没注册到 Edge
        return True, cid, None

    # ---- 主流程 ----
    async def deploy(self, prompt_id: str) -> DeployResult:
        t0 = time.time()
        result = DeployResult(success=False, uid=self._uid, conversation_log=self._log_path)
        used_prompts: set[str] = set()

        try:
            # phase 0: load prompt
            try:
                tpl = self._prompt_store.get(prompt_id)
            except KeyError as e:
                raise DeployError(VERIFY_FAILED, f"prompt 加载失败: {e}")
            used_prompts.add(prompt_id)
            result.prompt_id_used = prompt_id
            self._logger.info(f"[deploy] uid={self._uid} prompt={prompt_id} text_len={len(tpl.text)}")

            # phase 1: ensure_instance
            ok, wait_available, err = await self._retry("ensure", self._ensure_instance)
            if not ok:
                raise err
            # phase 2: ws_handshake
            async def _handshake():
                return await self._ws_handshake(wait_available)
            ok, _, err = await self._retry("ws", _handshake)
            if not ok:
                raise err
            self._logger.info("[deploy] WS 连接成功，开始注入")

            # phase 3+4: send + verify（deploy_refused 时换模板限量重试）
            swap_budget = 2  # 最多换 2 次模板
            current_tpl = tpl
            while True:
                async def _send():
                    try:
                        return await self._send_inject(current_tpl.text)
                    except DeployError as e:
                        if e.error_type == WS_DISCONNECTED:
                            self._logger.info("[send] WS 已断开，重新握手后再重试发送")
                            await self._ws_handshake(False)
                        raise
                ok, reply, err = await self._retry("send", _send, retryable_override={SEND_TIMEOUT, NETWORK_ERROR, WS_DISCONNECTED})
                if not ok:
                    raise err
                result.reply = reply
                success, cid, vet = await self._verify_reply(reply)
                if success:
                    result.success = True
                    result.connector_id = cid
                    break
                # 未通过——deploy_refused 则换模板
                if vet == DEPLOY_REFUSED and swap_budget > 0:
                    nxt = self._prompt_store.next_after(DEPLOY_REFUSED, used_prompts)
                    if nxt is not None:
                        self._logger.info(f"[deploy] deploy_refused，换模板 {nxt.prompt_id}")
                        current_tpl = nxt
                        result.prompt_id_used = nxt.prompt_id
                        used_prompts.add(nxt.prompt_id)
                        swap_budget -= 1
                        continue
                result.error_type = vet or VERIFY_FAILED
                result.error_detail = "verify 阶段未通过"
                break

        except DeployError as e:
            result.error_type = e.error_type
            result.error_detail = e.detail
            result.needs_relogin = needs_relogin(e.error_type)
        except Exception as e:
            result.error_type = NETWORK_ERROR
            result.error_detail = f"未预期异常: {e}"
            self._logger.exception(f"[deploy] 未预期异常: {e}")
        finally:
            try:
                await self.client.close()
            except Exception:
                pass
            # 最终实例状态
            try:
                st, remain, _ = await self._probe_status()
                result.instance_status = st
                result.instance_remain_sec = remain
            except Exception:
                pass
            result.attempts = dict(self._attempts)
            result.elapsed_sec = time.time() - t0

        self._logger.info(
            f"[deploy] 完成 uid={self._uid} success={result.success} "
            f"error={result.error_type} relogin={result.needs_relogin} "
            f"connector={result.connector_id} elapsed={result.elapsed_sec:.1f}s "
            f"attempts={result.attempts}"
        )
        return result


def _ALL_RETRYABLE() -> frozenset:
    """懒加载可重试类型集合（避免循环导入问题）。"""
    from deploy_errors import _RETRYABLE
    return _RETRYABLE
