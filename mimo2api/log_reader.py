from __future__ import annotations

import re
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LOGS_DIR = ROOT_DIR / "logs"
MAX_LOG_LINES = 1000
SAFE_LOG_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
ALLOWED_LOG_PATTERNS = (
    re.compile(r".*\.log(?:\.\d+)?$", re.IGNORECASE),
    re.compile(r".*\.txt$", re.IGNORECASE),
)
LEVEL_NAMES = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

SENSITIVE_PATTERNS = [
    re.compile(r"(?i)(Authorization\s*:\s*Bearer\s+)[^\s;,'\"]+"),
    re.compile(r"(?i)(Bearer\s+)[^\s;,'\"]+"),
    re.compile(r"(?i)(Cookie\s*:\s*)[^\n]+"),
    re.compile(
        r"(?i)\b(serviceToken|xiaomichatbot_ph|token|secret|session_secret|session|password|webui_password)\b"
        r"(\s*[:=]\s*[\"']?)[^\"'\s;,]+"
    ),
]


def redact_sensitive_text(text: str) -> str:
    redacted = text
    for pattern in SENSITIVE_PATTERNS:
        if pattern.groups >= 2:
            redacted = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", redacted)
        else:
            redacted = pattern.sub(lambda m: f"{m.group(1)}[REDACTED]", redacted)
    return redacted


def _is_allowed_log_name(filename: str) -> bool:
    if not filename or not SAFE_LOG_NAME.fullmatch(filename):
        return False
    return any(pattern.fullmatch(filename) for pattern in ALLOWED_LOG_PATTERNS)


def _resolve_log_path(logs_dir: Path | str, filename: str) -> Path:
    if not _is_allowed_log_name(filename):
        raise ValueError("invalid log file name")

    root = Path(logs_dir).resolve()
    path = (root / filename).resolve()
    if not path.is_relative_to(root):
        raise ValueError("log file must stay inside logs directory")
    if not path.is_file():
        raise FileNotFoundError(filename)
    return path


def list_log_files(*, logs_dir: Path | str = DEFAULT_LOGS_DIR) -> dict[str, Any]:
    root = Path(logs_dir)
    files: list[dict[str, Any]] = []
    if not root.exists():
        return {"files": [], "default_file": None}

    for path in root.iterdir():
        if not path.is_file() or not _is_allowed_log_name(path.name):
            continue
        stat = path.stat()
        files.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "modified_at": stat.st_mtime,
            }
        )

    files.sort(key=lambda item: (item["modified_at"], item["name"]), reverse=True)
    default_file = "gateway.log" if any(item["name"] == "gateway.log" for item in files) else (files[0]["name"] if files else None)
    return {"files": files, "default_file": default_file}


def _normalize_limit(limit: int | str | None) -> int:
    try:
        value = int(limit if limit is not None else 200)
    except (TypeError, ValueError):
        value = 200
    return max(1, min(value, MAX_LOG_LINES))


def _line_matches(line: str, *, level: str = "", keyword: str = "") -> bool:
    normalized_level = str(level or "").strip().upper()
    if normalized_level:
        if normalized_level not in LEVEL_NAMES:
            return False
        if not re.search(rf"\b{re.escape(normalized_level)}\b", line, re.IGNORECASE):
            return False

    normalized_keyword = str(keyword or "").strip().lower()
    if normalized_keyword and normalized_keyword not in line.lower():
        return False
    return True


def read_log_file(
    *,
    logs_dir: Path | str = DEFAULT_LOGS_DIR,
    filename: str = "gateway.log",
    limit: int | str | None = 200,
    level: str = "",
    keyword: str = "",
) -> dict[str, Any]:
    path = _resolve_log_path(logs_dir, filename)
    line_limit = _normalize_limit(limit)
    try:
        raw_lines = path.read_text("utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise ValueError(f"failed to read log file: {exc}") from exc

    filtered = [
        redact_sensitive_text(line)
        for line in raw_lines
        if _line_matches(line, level=level, keyword=keyword)
    ]
    lines = filtered[-line_limit:]
    return {
        "file": path.name,
        "limit": line_limit,
        "level": str(level or "").strip().upper(),
        "keyword": str(keyword or "").strip(),
        "count": len(lines),
        "total_matched": len(filtered),
        "lines": lines,
    }
