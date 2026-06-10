#!/usr/bin/env python3
"""
mimo2api 系统统一个化主入口

启动前只需修改此处的全局配置。
"""
import os
import sys
import logging
import asyncio
import uvicorn
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

load_dotenv()

from mimo2api.logging_utils import (
    LoggingSettings,
    apply_library_log_levels,
    parse_bool,
    resolve_log_level,
)
from mimo2api.runtime_config import effective_ws_url, get_config_value, sync_bridge_ws_env

# ================= 统一全局配置（优先读 .env，有默认值兜底） =================
SERVER_HOST = str(get_config_value("server.host", "0.0.0.0"))
SERVER_PORT = int(get_config_value("server.port", 8000))
WS_TUNNEL_URL = effective_ws_url()
# ================================================

sync_bridge_ws_env(WS_TUNNEL_URL)

# 引入实际带 Lifespan 背景挂载服务的 FastAPI APP 对象
from mimo2api.web_service import app


def shutdown_timeout_seconds() -> int:
    try:
        return max(1, int(float(os.getenv("MIMO_SHUTDOWN_TASK_TIMEOUT", "5"))))
    except ValueError:
        return 5


def configure_event_loop_policy() -> None:
    if os.name != "nt" or not parse_bool(os.getenv("MIMO_WINDOWS_SELECTOR_LOOP"), default=True):
        return
    selector_policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if selector_policy is not None:
        asyncio.set_event_loop_policy(selector_policy())


def configure_logging(
    *,
    log_dir: str | os.PathLike[str] | None = None,
    log_level: str | None = None,
    access_log: bool | str | None = None,
) -> LoggingSettings:
    level_name, level = resolve_log_level(log_level or os.getenv("MIMO_LOG_LEVEL"), "INFO")
    access_log_enabled = parse_bool(
        os.getenv("MIMO_ACCESS_LOG"),
        default=False,
    ) if access_log is None else parse_bool(access_log, default=False)
    resolved_log_dir = os.fspath(log_dir) if log_dir is not None else os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "logs",
    )
    os.makedirs(resolved_log_dir, exist_ok=True)
    log_file = os.path.join(resolved_log_dir, "gateway.log")

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    fmt = logging.Formatter("%(asctime)s - [%(name)s] - %(levelname)s - %(message)s")

    # journal（stdout）
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root_logger.addHandler(sh)

    # 文件日志 — 10MB × 5 个轮转，不截断长行
    fh = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root_logger.addHandler(fh)

    apply_library_log_levels(access_log_enabled=access_log_enabled)
    return LoggingSettings(
        log_level_name=level_name,
        log_level=level,
        access_log_enabled=access_log_enabled,
        log_file=log_file,
    )


def run_gateway() -> None:
    configure_event_loop_policy()
    settings = configure_logging()
    logging.info(f"🚀 mimo2api 统一主入口 - 正在启动网关并绑定集群到 {SERVER_HOST}:{SERVER_PORT}")
    logging.info(f"🔗 云端要求 Claw 主动连接的桥接 WS URL 将统一下发为: {WS_TUNNEL_URL}")
    uvicorn.run(
        app,
        host=SERVER_HOST,
        port=SERVER_PORT,
        ws_max_size=10**8,
        access_log=settings.access_log_enabled,
        timeout_graceful_shutdown=shutdown_timeout_seconds(),
    )


if __name__ == "__main__":
    run_gateway()
