"""Docker/runtime entrypoint helpers for the WebUI service."""

from __future__ import annotations

import os

from .config import settings


DEFAULT_WEBUI_HOST = "0.0.0.0"
DEFAULT_WEBUI_PORT = 8358


def _port(value: object, default: int = DEFAULT_WEBUI_PORT) -> int:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default


def resolve_webui_bind() -> tuple[str, int]:
    cfg = settings()
    webui = cfg.get("webui", {}) if isinstance(cfg, dict) else {}
    host = str(webui.get("host") or DEFAULT_WEBUI_HOST).strip() or DEFAULT_WEBUI_HOST
    port = _port(webui.get("port"), DEFAULT_WEBUI_PORT)
    return host, port


def main() -> None:
    host, port = resolve_webui_bind()
    os.execvp(
        "uv",
        [
            "uv",
            "run",
            "--frozen",
            "uvicorn",
            "webui.server:app",
            "--host",
            host,
            "--port",
            str(port),
        ],
    )


if __name__ == "__main__":
    main()
