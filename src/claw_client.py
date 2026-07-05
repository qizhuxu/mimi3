#!/usr/bin/env python3
"""
mimi3-n · claw_client.py

从 mimi3 (mimo2api/manager.py) 提取的 NativeClawClient，自包含依赖。

职责:
1. 通过 aistudio.xiaomimimo.com open-apis 创建/查询 mimo-claw 实例
2. 建立 WebSocket、监听事件、发送 chat 消息、捕获最终 AI 文本回复
3. close 优雅清理

对外接口（与 mimi3 一致）:
    NativeClawClient(ph, cookies, logger)
    await client.connect(wait_available=True) -> bool
    await client.send_message(text, timeout=120, stage="chat", prompt_id=None) -> str
    await client.close()

依赖: 标准库 + httpx + websockets
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from typing import Any
from urllib.parse import quote

import httpx
import websockets

# ----------------- 常量 -----------------

BASE_URL = "https://aistudio.xiaomimimo.com"
WS_URL = "wss://aistudio.xiaomimimo.com/ws/proxy"
DEFAULT_TEXT_LIMIT = int(os.getenv("MIMO_LOG_TEXT_LIMIT", "360") or 360)

# ----------------- 日志 / 文本工具 -----------------


def compact_text(value: Any, limit: int | None = None) -> str:
    text_limit = DEFAULT_TEXT_LIMIT if limit is None else max(20, int(limit))
    if value is None:
        return "<none>"
    text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    text = re.sub(r"[ \t\f\v]+", " ", text).strip()
    if len(text) <= text_limit:
        return text
    digest = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:10]
    truncated = text[:text_limit].rstrip()
    return f"{truncated}... [truncated_chars={len(text) - text_limit} sha1={digest}]"


def format_event(event: str, *, text_limit: int | None = None, **fields: Any) -> str:
    parts = [f"event={event}"]
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (int, float)):
            rendered = str(value)
        else:
            rendered = compact_text(value, text_limit)
            if rendered == "":
                continue
            if re.search(r"\s|=", rendered):
                rendered = json.dumps(rendered, ensure_ascii=False)
        parts.append(f"{key}={rendered}")
    return " ".join(parts)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    text_limit: int | None = None,
    **fields: Any,
) -> None:
    logger.log(level, format_event(event, text_limit=text_limit, **fields))


def _aistudio_headers() -> dict:
    return {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "x-timezone": "Asia/Shanghai",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }


def _truncate_text(value: Any, limit: int = 300) -> str:
    return compact_text(value, limit=limit)


_SECRET_TEXT_SUBS = (
    (re.compile(r"(?i)(cookie\s*:\s*)[^\n\r]+"), r"\1<redacted>"),
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+"), r"\1<redacted>"),
    (
        re.compile(
            r"(?i)((?:serviceToken|xiaomichatbot_ph|session_secret|webui_session)\s*=\s*)[\"']?[^;\s,\"']+"
        ),
        r"\1<redacted>",
    ),
    (
        re.compile(
            r"(?i)(\"(?:serviceToken|xiaomichatbot_ph|session_secret|webui_session)\"\s*:\s*\")[^\"]+(\")"
        ),
        r"\1<redacted>\2",
    ),
    (
        re.compile(
            r"(?i)('(?:serviceToken|xiaomichatbot_ph|session_secret|webui_session)'\s*:\s*')[^']+(')"
        ),
        r"\1<redacted>\2",
    ),
    (
        re.compile(r"(?i)((?:MIMO_API_KEY|MIMO_API_ENDPOINT|TUNNEL_TOKEN|PROXY_API_KEY|CF_API_TOKEN|CF_ACCOUNT_ID)\s*=\s*)[\"']?[^;\s,\"']+"),
        r"\1<redacted>",
    ),
    (
        re.compile(
            r"(?i)(\"(?:MIMO_API_KEY|MIMO_API_ENDPOINT|TUNNEL_TOKEN|PROXY_API_KEY|CF_API_TOKEN|CF_ACCOUNT_ID|api-key)\"\s*:\s*\")[^\"]+(\")"
        ),
        r"\1<redacted>\2",
    ),
    (
        re.compile(
            r"(?i)('(?:MIMO_API_KEY|MIMO_API_ENDPOINT|TUNNEL_TOKEN|PROXY_API_KEY|CF_API_TOKEN|CF_ACCOUNT_ID|api-key)'\s*:\s*')[^']+(')"
        ),
        r"\1<redacted>\2",
    ),
)


def safe_claw_trace_text(value: Any, limit: int = 360) -> str:
    text = "" if value is None else str(value)
    for pattern, replacement in _SECRET_TEXT_SUBS:
        text = pattern.sub(replacement, text)
    return compact_text(text, limit=limit)


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


# ----------------- Native Claw Client 实现 -----------------


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
        # create API 失败的精确原因（reason/http_status/api_code/detail），供 deployer 区分 7001 等。
        # None = 未进入 _create_and_wait 或本次 create 成功。
        self.last_create_error: dict | None = None

    async def get_instance_status(self) -> tuple[str, int]:
        """查询当前 Claw 实例状态和剩余时间(秒)。状态为空表示无实例或查询失败。"""
        url = f"{BASE_URL}/open-apis/user/mimo-claw/status"
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(
                    url, cookies=self.cookies, headers=_aistudio_headers(), timeout=15
                )
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

    async def _create_and_wait(self) -> bool:
        """创建 Claw 实例并等待其可用。失败时设 self.last_create_error 供调用方分类（7001 等）。"""
        url_create = f"{BASE_URL}/open-apis/user/mimo-claw/create?xiaomichatbot_ph={quote(self.ph)}"
        url_status = f"{BASE_URL}/open-apis/user/mimo-claw/status"
        url_agree = f"{BASE_URL}/open-apis/agreement/user/mimo-claw?xiaomichatbot_ph={quote(self.ph)}"
        self.last_create_error = None  # 每次进入先清，避免上次残留

        async with httpx.AsyncClient() as client:
            # 1. 尝试签署 agreement
            try:
                agree_resp = await client.post(
                    url_agree, cookies=self.cookies, headers=_aistudio_headers(), timeout=15
                )
                agree_data, agree_detail = _response_details(agree_resp)
                if agree_resp.status_code >= 400 or (
                    isinstance(agree_data, dict) and agree_data.get("code") not in (None, 0)
                ):
                    level = (
                        logging.DEBUG
                        if isinstance(agree_data, dict) and agree_data.get("code") == 2007
                        else logging.WARNING
                    )
                    log_event(
                        self.logger,
                        level,
                        "claw.agreement.result",
                        uid=self.cookies.get("userId"),
                        detail=agree_detail,
                        text_limit=240,
                    )
            except Exception as e:
                self.logger.warning(f"签署 agreement 异常: {e}")

            # 2. 发起创建。HTTP 429 是高峰期临时限流，短时间内重试 3 次。
            for create_attempt in range(3):
                r = await client.post(
                    url_create, cookies=self.cookies, headers=_aistudio_headers(), timeout=20
                )
                create_data, create_detail = _response_details(r)
                api_code = create_data.get("code") if isinstance(create_data, dict) else None
                if r.status_code != 429 or api_code == 7001:
                    break
                log_event(
                    self.logger,
                    logging.WARNING,
                    "claw.create.peak_rate_limited",
                    uid=self.cookies.get("userId"),
                    attempt=create_attempt + 1,
                    detail=create_detail,
                    text_limit=240,
                )
                if create_attempt < 2:
                    await asyncio.sleep(3)
            if r.status_code == 401:
                self.last_create_error = {"reason": "auth_expired", "http_status": 401,
                                          "api_code": create_data.get("code") if isinstance(create_data, dict) else None,
                                          "detail": create_detail}
                self.logger.error(f"账户已过期失效: {create_detail}")
                return False
            if isinstance(create_data, dict) and create_data.get("code") == 7001:
                self.last_create_error = {"reason": "api_code_error", "http_status": r.status_code,
                                          "api_code": create_data.get("code"),
                                          "detail": create_detail}
                self.logger.error(f"创建实例接口返回 7001: {create_detail}")
                return False
            if r.status_code == 429:
                self.last_create_error = {"reason": "peak_rate_limited", "http_status": 429,
                                          "api_code": create_data.get("code") if isinstance(create_data, dict) else None,
                                          "detail": create_detail}
                self.logger.error(f"当前 Claw 实例负载过高: {create_detail}")
                return False
            if r.status_code >= 400:
                self.last_create_error = {"reason": "http_error", "http_status": r.status_code,
                                          "api_code": create_data.get("code") if isinstance(create_data, dict) else None,
                                          "detail": create_detail}
                self.logger.error(f"创建实例请求失败: {create_detail}")
                return False
            if isinstance(create_data, dict) and create_data.get("code") not in (None, 0):
                self.last_create_error = {"reason": "api_code_error", "http_status": r.status_code,
                                          "api_code": create_data.get("code"),
                                          "detail": create_detail}
                self.logger.error(f"创建实例接口返回异常: {create_detail}")
                return False

            # 3. 轮询直到 AVAILABLE
            deadline = time.time() + 120
            last_status = None
            last_status_detail = "未拿到状态详情"
            while time.time() < deadline:
                sr = await client.get(
                    url_status, cookies=self.cookies, headers=_aistudio_headers(), timeout=15
                )
                if sr.status_code == 401:
                    _, status_detail = _response_details(sr)
                    self.last_create_error = {"reason": "auth_expired", "http_status": 401,
                                              "api_code": None, "detail": status_detail}
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
                        self.last_create_error = {"reason": "terminal_status", "http_status": 200,
                                                  "api_code": None, "detail": f"status={st} {status_detail}"}
                        self.logger.error(f"创建失败，状态进入终态: {status_detail}")
                        return False
                except Exception as e:
                    self.logger.warning(f"解析创建状态异常: {e}")
                await asyncio.sleep(2)
        self.last_create_error = {"reason": "timeout", "http_status": 200,
                                  "api_code": None, "detail": f"120s 未到 AVAILABLE，最后: {last_status_detail}"}
        self.logger.error(f"创建实例等待超时，最后状态: {last_status_detail}")
        return False

    async def _get_ticket(self) -> str:
        """获取建立 ws 需要的 ticket"""
        url = f"{BASE_URL}/open-apis/user/ws/ticket?xiaomichatbot_ph={quote(self.ph)}"
        async with httpx.AsyncClient() as client:
            detail = "<no response>"
            for attempt in range(5):
                r = await client.get(
                    url, cookies=self.cookies, headers=_aistudio_headers(), timeout=15
                )
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

    async def connect(self, wait_available: bool = True) -> bool:
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

        cookie_str = "; ".join(
            f'{k}="{v}"' if " " in v or "=" in v else f"{k}={v}" for k, v in self.cookies.items()
        )
        headers_dict = {"Cookie": cookie_str, "Origin": BASE_URL}

        try:
            # 兼容 python websockets >= 14.0
            try:
                self.ws = await websockets.connect(
                    f"{WS_URL}?ticket={ticket}", additional_headers=headers_dict
                )
            except TypeError as e:
                if "additional_headers" in str(e):
                    self.ws = await websockets.connect(
                        f"{WS_URL}?ticket={ticket}", extra_headers=headers_dict
                    )
                else:
                    raise
        except Exception as e:
            self.logger.error(f"WebSocket 连结失败: {e}")
            return False

        self.connected = False
        self._listen_task = asyncio.create_task(
            self._ws_loop(), name=f"claw-listener-{self.logger.name}"
        )

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
                    await self.ws.send(
                        json.dumps(
                            {
                                "type": "req",
                                "id": str(uuid.uuid4()),
                                "method": "connect",
                                "params": {
                                    "minProtocol": 4,
                                    "maxProtocol": 4,
                                    "client": {
                                        "id": "cli",
                                        "version": "mimo-claw-ui",
                                        "platform": "Linux x86_64",
                                        "mode": "cli",
                                    },
                                    "role": "operator",
                                    "scopes": [
                                        "operator.admin",
                                        "operator.read",
                                        "operator.write",
                                        "operator.approvals",
                                        "operator.pairing",
                                    ],
                                    "caps": ["tool-events"],
                                    "userAgent": "Mozilla/5.0",
                                    "locale": "zh-CN",
                                },
                            }
                        )
                    )
                elif data["type"] == "res":
                    self.responses[data["id"]] = data
                    if data.get("ok") and data.get("payload", {}).get("type") == "hello-ok":
                        self.connected = True
                elif data["type"] == "event":
                    self.events.append(data)
        except Exception:
            self.connected = False

    async def send_message(
        self,
        text: str,
        timeout: int = 120,
        stage: str = "chat",
        prompt_id: str | None = None,
    ) -> str:
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
            "type": "req",
            "id": req_id,
            "method": "chat.send",
            "params": {
                "sessionKey": self.session_key,
                "message": text,
                "idempotencyKey": str(uuid.uuid4()),
            },
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
            for evt in list(self.events):  # 复制一份遍历避免动态更改引发异常
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
