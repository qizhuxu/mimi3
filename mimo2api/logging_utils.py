from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any


DEFAULT_TEXT_LIMIT = int(os.getenv("MIMO_LOG_TEXT_LIMIT", "360") or 360)
_RATE_LIMIT_STATE: dict[str, float] = {}


@dataclass(frozen=True)
class LoggingSettings:
    log_level_name: str
    log_level: int
    access_log_enabled: bool
    log_file: str | None


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def resolve_log_level(value: str | None, default: str = "INFO") -> tuple[str, int]:
    name = (value or default or "INFO").strip().upper()
    level = logging.getLevelName(name)
    if isinstance(level, int):
        return name, level
    return default.upper(), int(logging.getLevelName(default.upper()))


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


def log_event_rate_limited(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    key: str | None = None,
    interval_seconds: float = 30.0,
    text_limit: int | None = None,
    **fields: Any,
) -> bool:
    rate_key = key or format_event(event, text_limit=text_limit, **fields)
    now = time.monotonic()
    last_at = _RATE_LIMIT_STATE.get(rate_key, 0.0)
    if now - last_at < interval_seconds:
        return False
    _RATE_LIMIT_STATE[rate_key] = now
    log_event(logger, level, event, text_limit=text_limit, **fields)
    return True


def configure_uvicorn_access_logging(access_log_enabled: bool) -> None:
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.disabled = not access_log_enabled
    access_logger.setLevel(logging.INFO if access_log_enabled else logging.WARNING)


def apply_library_log_levels(*, access_log_enabled: bool) -> None:
    configure_uvicorn_access_logging(access_log_enabled)
    for logger_name in ("httpx", "httpcore", "websockets.client", "websockets.server"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)

