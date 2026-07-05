#!/usr/bin/env python3
"""
mimi3-n · test_inject.py

用 user_6874275245.json 凭据测试 claw_client 完整流程：
  1. 加载凭据 → NativeClawClient.connect()（创建实例 + WS 握手）
  2. 发注入 skill 的提示词（操作员提供）
  3. 录制全部 WS 进出消息到 conversation_log.jsonl
  4. 落盘最终回复 + 人类可读摘要

用法: python test_inject.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

# ----------------- 配置 -----------------

_SRC = Path(__file__).resolve().parent.parent / "src"  # test/../src/
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from claw_client import NativeClawClient

BASE_DIR = Path(__file__).resolve().parent.parent  # test/.. → 项目根
DEFAULT_USER_FILE = BASE_DIR / "data" / "creds" / "user_6877172098.json"


def _load_inject_prompt(prompt_file: Path | None) -> str:
    if prompt_file is not None:
        with open(prompt_file, "r", encoding="utf-8") as _f:
            return _f.read().strip()

    from prompt_store import PromptStore

    _store = PromptStore(BASE_DIR / "data" / "prompts" / "templates.json")
    return _store.get("deploy.v1.standard").text.strip()


def _runtime_inputs(argv: list[str]) -> tuple[Path, str, Path, Path]:
    # Keep file I/O out of module import so unittest discovery can import this script.
    user_file = Path(argv[1]) if len(argv) > 1 else DEFAULT_USER_FILE
    prompt_file = Path(argv[2]) if len(argv) > 2 else None
    user_stem = user_file.stem
    conversation_log = BASE_DIR / "data" / "logs" / f"conversation_log_{user_stem}.jsonl"
    summary_log = BASE_DIR / "data" / "logs" / f"conversation_summary_{user_stem}.md"
    return user_file, _load_inject_prompt(prompt_file), conversation_log, summary_log

# 部署任务很重（git clone + 下载二进制 + 跑 deploy + 验证），给足超时
SEND_TIMEOUT = 900  # 15 分钟


# ----------------- 录制客户端 -----------------


class RecordingClawClient(NativeClawClient):
    """在 _ws_loop 之外录制每条 WS 进出消息到 JSONL。"""

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
            line = {
                "seq": self._msg_seq,
                "ts": time.time(),
                "dir": direction,  # "in" / "out"
                "data": data,
            }
            self._record_file.write(json.dumps(line, ensure_ascii=False) + "\n")
            self._record_file.flush()

    async def _ws_loop(self):
        """覆盖：复制父类握手逻辑，在每条 incoming 上加录制。"""
        try:
            async for message in self.ws:
                try:
                    data = json.loads(message)
                except Exception:
                    data = {"_raw": message}
                self._record("in", data)

                if isinstance(data, dict):
                    if (
                        data.get("type") == "event"
                        and data.get("event") == "connect.challenge"
                    ):
                        handshake = {
                            "type": "req",
                            "id": str(__import__("uuid").uuid4()),
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
                        await self.ws.send(json.dumps(handshake))
                        self._record("out", handshake)
                    elif data.get("type") == "res":
                        self.responses[data["id"]] = data
                        if (
                            data.get("ok")
                            and data.get("payload", {}).get("type") == "hello-ok"
                        ):
                            self.connected = True
                    elif data.get("type") == "event":
                        self.events.append(data)
        except Exception:
            self.connected = False

    async def connect(self, wait_available: bool = True) -> bool:
        self._open_record()
        ok = await super().connect(wait_available=wait_available)
        # 包装 ws.send 录制 outgoing（之后的 chat.send 等也会被录到）
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


# ----------------- 主流程 -----------------


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


def build_logger() -> logging.Logger:
    logger = logging.getLogger("claw-test")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        # Windows 默认 GBK，emoji/中文会崩；强制 UTF-8 输出
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(h)
    return logger


async def main():
    logger = build_logger()
    user_file, inject_prompt, conversation_log, summary_log = _runtime_inputs(sys.argv)
    user = load_credentials(user_file)
    uid = user.get("userId", "")
    ph = user.get("xiaomichatbot_ph", "")
    cookies = {
        "serviceToken": user.get("serviceToken", ""),
        "userId": uid,
        "xiaomichatbot_ph": ph,
    }
    name = user.get("name", uid)

    # 清旧日志
    if conversation_log.exists():
        conversation_log.unlink()
    if summary_log.exists():
        summary_log.unlink()

    logger.info(f"=== claw 注入测试开始 uid={uid} name={name} ===")
    logger.info(f"凭据文件: {user_file}")
    logger.info(f"对话日志: {conversation_log}")

    client = RecordingClawClient(
        ph=ph, cookies=cookies, logger_obj=logger, record_path=conversation_log
    )

    t0 = time.time()
    try:
        logger.info(">>> Step 1: connect (创建实例 + WS 握手)")
        # 先查状态：已 AVAILABLE 就跳过创建（避免触发 24h 创建额度限制）
        status, remain = await client.get_instance_status()
        logger.info(f"    当前实例状态: status={status!r} 剩余={remain}s")
        if status == "AVAILABLE":
            logger.info("    实例已可用，跳过创建，直接连 WS")
            connected = await client.connect(wait_available=False)
        else:
            connected = await client.connect(wait_available=True)
        logger.info(f"<<< connect 结果: {connected} (耗时 {time.time()-t0:.1f}s)")
        if not connected:
            logger.error("连接失败，终止")
            return 1

        logger.info(">>> Step 2: send_message 注入 skill 提示词")
        t1 = time.time()
        reply = await client.send_message(
            inject_prompt,
            timeout=SEND_TIMEOUT,
            stage="skill.inject",
            prompt_id="mimo-proxy-deploy.v1",
        )
        elapsed = time.time() - t1
        logger.info(f"<<< send_message 返回 (耗时 {elapsed:.1f}s)")

        # 写摘要
        with open(summary_log, "w", encoding="utf-8") as f:
            f.write("# Claw 注入测试摘要\n\n")
            f.write(f"- uid: {uid}\n- name: {name}\n")
            f.write(f"- 连接耗时: {time.time()-t0:.1f}s\n")
            f.write(f"- 注入回复耗时: {elapsed:.1f}s\n")
            f.write(f"- 对话日志: {conversation_log.name}\n\n")
            f.write("## 注入提示词\n\n```\n")
            f.write(inject_prompt)
            f.write("\n```\n\n")
            f.write("## Claw 最终回复\n\n```\n")
            f.write(reply or "(空)")
            f.write("\n```\n")
        logger.info(f"摘要已写入: {summary_log}")
        logger.info(f"对话日志: {conversation_log} ({conversation_log.stat().st_size} bytes)")
        logger.info(f"=== 最终回复预览 ===\n{reply[:500] if reply else '(空)'}")
        return 0
    except Exception as e:
        logger.exception(f"测试异常: {e}")
        return 2
    finally:
        logger.info(">>> close")
        await client.close()
        logger.info(f"=== 全流程结束 总耗时 {time.time()-t0:.1f}s ===")


if __name__ == "__main__":
    rc = asyncio.run(main())
    sys.exit(rc)
