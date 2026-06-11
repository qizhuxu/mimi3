import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_RUNTIME_CONFIG_PATH = ROOT_DIR / "data" / "runtime_config.json"
RUNTIME_CONFIG_PATH_ENV = "MIMO_RUNTIME_CONFIG_PATH"
CONFIG_SOURCE_ENV = "MIMO_CONFIG_SOURCE"
ACTIVE_TUNNEL_WS_ENV = "MIMO_TUNNEL_ACTIVE_WS_URL"
BRIDGE_WS_ENV = "MIMO2API_WS_URL"


@dataclass(frozen=True, slots=True)
class ConfigField:
    key: str
    env: str
    default: Any | Callable[[], Any]
    value_type: str = "str"
    sensitive: bool = False
    requires_restart: bool = False
    label: str = ""
    group: str = ""


_runtime_config_lock = threading.RLock()
_runtime_config_cache: dict[str, Any] | None = None


def _runtime_config_path() -> Path:
    raw_path = os.getenv(RUNTIME_CONFIG_PATH_ENV, "").strip()
    return Path(raw_path) if raw_path else DEFAULT_RUNTIME_CONFIG_PATH


def _config_source_policy() -> str:
    value = os.getenv(CONFIG_SOURCE_ENV, "auto").strip().lower()
    return value if value in {"auto", "env", "ui"} else "auto"


def _default_server_host() -> str:
    return "0.0.0.0"


def _default_server_port() -> int:
    return 8000


def _default_ws_url() -> str:
    host = str(_raw_runtime_config().get("server.host") or os.getenv("SERVER_HOST") or _default_server_host())
    port = _raw_runtime_config().get("server.port") or os.getenv("SERVER_PORT") or _default_server_port()
    return f"ws://{host}:{port}/ws"


FIELDS: dict[str, ConfigField] = {
    "server.host": ConfigField("server.host", "SERVER_HOST", _default_server_host, "str", requires_restart=True, label="监听地址", group="访问与网关"),
    "server.port": ConfigField("server.port", "SERVER_PORT", _default_server_port, "int", requires_restart=True, label="监听端口", group="访问与网关"),
    "gateway.ws_tunnel_url": ConfigField("gateway.ws_tunnel_url", "WS_TUNNEL_URL", _default_ws_url, "str", label="Bridge WebSocket 地址", group="访问与网关"),
    "gateway.public_base_url": ConfigField("gateway.public_base_url", "MIMO_PUBLIC_BASE_URL", "", "str", label="公网 HTTP 地址", group="访问与网关"),
    "webui.username": ConfigField("webui.username", "MIMO_WEBUI_USERNAME", "admin", "str", label="WebUI 用户名", group="WebUI 登录"),
    "webui.password": ConfigField("webui.password", "MIMO_WEBUI_PASSWORD", "", "str", sensitive=True, label="WebUI 密码", group="WebUI 登录"),
    "webui.secret": ConfigField("webui.secret", "MIMO_WEBUI_SECRET", "", "str", sensitive=True, label="会话签名密钥", group="WebUI 登录"),
    "webui.session_ttl_seconds": ConfigField("webui.session_ttl_seconds", "MIMO_WEBUI_SESSION_TTL_SECONDS", 43200, "int", label="会话有效期", group="WebUI 登录"),
    "webui.cookie_secure": ConfigField("webui.cookie_secure", "MIMO_WEBUI_COOKIE_SECURE", False, "bool", label="Cookie Secure", group="WebUI 登录"),
    "api.openai_key": ConfigField("api.openai_key", "MIMO_RELAY_OPENAI_KEY", "", "str", sensitive=True, label="OpenAI 兼容 API Key", group="API 鉴权"),
    "lifecycle.monitor_interval_seconds": ConfigField("lifecycle.monitor_interval_seconds", "MIMO_LIFECYCLE_MONITOR_INTERVAL_SECONDS", 30, "int", label="监测间隔", group="生命周期策略"),
    "lifecycle.node_stale_seconds": ConfigField("lifecycle.node_stale_seconds", "MIMO_LIFECYCLE_NODE_STALE_SECONDS", 90, "int", label="节点失联阈值", group="生命周期策略"),
    "lifecycle.auto_rebuild": ConfigField("lifecycle.auto_rebuild", "MIMO_LIFECYCLE_AUTO_REBUILD", False, "bool", label="异常自动重建", group="生命周期策略"),
    "lifecycle.auto_rebuild_failures": ConfigField("lifecycle.auto_rebuild_failures", "MIMO_LIFECYCLE_AUTO_REBUILD_FAILURES", 3, "int", label="自动重建失败阈值", group="生命周期策略"),
    "lifecycle.initial_stagger_window_seconds": ConfigField("lifecycle.initial_stagger_window_seconds", "MIMO_LIFECYCLE_INITIAL_STAGGER_WINDOW_SECONDS", 1800, "int", label="初始错峰窗口", group="生命周期策略"),
    "lifecycle.fast_start_count": ConfigField("lifecycle.fast_start_count", "MIMO_LIFECYCLE_FAST_START_COUNT", 1, "int", label="快速启动账号数", group="生命周期策略"),
    "lifecycle.min_available_nodes": ConfigField("lifecycle.min_available_nodes", "MIMO_LIFECYCLE_MIN_AVAILABLE_NODES", 1, "int", label="最小可用节点数", group="生命周期策略"),
    "lifecycle.max_parallel_rebuilds": ConfigField("lifecycle.max_parallel_rebuilds", "MIMO_LIFECYCLE_MAX_PARALLEL_REBUILDS", 1, "int", label="最大并行重建数", group="生命周期策略"),
    "lifecycle.rebuild_wait_seconds": ConfigField("lifecycle.rebuild_wait_seconds", "MIMO_LIFECYCLE_REBUILD_WAIT_SECONDS", 30, "int", label="重建等待间隔", group="生命周期策略"),
    "tunnel.mode": ConfigField("tunnel.mode", "MIMO_TUNNEL_MODE", "none", "str", label="隧道模式", group="Cloudflare Tunnel"),
    "tunnel.cloudflared_bin": ConfigField("tunnel.cloudflared_bin", "MIMO_CLOUDFLARED_BIN", "cloudflared", "str", label="cloudflared 路径", group="Cloudflare Tunnel"),
    "tunnel.cloudflare_tunnel_token": ConfigField("tunnel.cloudflare_tunnel_token", "MIMO_CLOUDFLARE_TUNNEL_TOKEN", "", "str", sensitive=True, label="固定隧道 Token", group="Cloudflare Tunnel"),
    "tunnel.cloudflare_public_hostname": ConfigField("tunnel.cloudflare_public_hostname", "MIMO_CLOUDFLARE_PUBLIC_HOSTNAME", "", "str", label="固定隧道域名", group="Cloudflare Tunnel"),
}


def _raw_runtime_config() -> dict[str, Any]:
    global _runtime_config_cache
    with _runtime_config_lock:
        if _runtime_config_cache is not None:
            return _runtime_config_cache

        path = _runtime_config_path()
        try:
            data = json.loads(path.read_text("utf-8")) if path.exists() else {}
        except (OSError, json.JSONDecodeError):
            data = {}
        _runtime_config_cache = data if isinstance(data, dict) else {}
        return _runtime_config_cache


def reload_runtime_config() -> None:
    global _runtime_config_cache
    with _runtime_config_lock:
        _runtime_config_cache = None


def _coerce_value(field: ConfigField, raw_value: Any) -> Any:
    if raw_value is None:
        return None
    if field.value_type == "bool":
        if isinstance(raw_value, bool):
            return raw_value
        return str(raw_value).strip().lower() in {"1", "true", "yes", "on"}
    if field.value_type == "int":
        try:
            return int(raw_value)
        except (TypeError, ValueError):
            default = field.default() if callable(field.default) else field.default
            return int(default)
    return str(raw_value)


def _field_default(field: ConfigField) -> Any:
    default = field.default() if callable(field.default) else field.default
    return _coerce_value(field, default)


def _field_from_env(field: ConfigField) -> Any:
    return _coerce_value(field, os.getenv(field.env, ""))


def _field_from_runtime(field: ConfigField) -> Any:
    return _coerce_value(field, _raw_runtime_config().get(field.key))


def _effective_source_and_value(field: ConfigField) -> tuple[str, Any]:
    policy = _config_source_policy()
    env_set = field.env in os.environ
    runtime_data = _raw_runtime_config()
    runtime_set = field.key in runtime_data

    if policy == "env":
        if env_set:
            return "env", _field_from_env(field)
        return "default", _field_default(field)

    if policy == "ui":
        if runtime_set:
            return "runtime_config", _field_from_runtime(field)
        if env_set:
            return "env", _field_from_env(field)
        return "default", _field_default(field)

    if env_set:
        return "env", _field_from_env(field)
    if runtime_set:
        return "runtime_config", _field_from_runtime(field)
    return "default", _field_default(field)


def is_field_editable(field: ConfigField) -> bool:
    policy = _config_source_policy()
    if policy == "env":
        return False
    if policy == "auto" and field.env in os.environ:
        return False
    return True


def get_config_value(key: str, default: Any = None) -> Any:
    field = FIELDS.get(key)
    if field is None:
        return default
    _, value = _effective_source_and_value(field)
    return default if value is None else value


def get_config_metadata() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "_meta": {
            "config_source": _config_source_policy(),
            "config_path": str(_runtime_config_path()),
        }
    }
    for key, field in FIELDS.items():
        source, value = _effective_source_and_value(field)
        item = {
            "source": source,
            "editable": is_field_editable(field),
            "requires_restart": field.requires_restart,
            "sensitive": field.sensitive,
            "type": field.value_type,
            "label": field.label,
            "group": field.group,
            "env": field.env,
        }
        if field.sensitive:
            item["configured"] = bool(value)
        else:
            item["value"] = value
        payload[key] = item
    return payload


def _extract_update_value(raw_item: Any) -> tuple[bool, Any]:
    if isinstance(raw_item, dict):
        if raw_item.get("clear") is True:
            return True, None
        return False, raw_item.get("value")
    return False, raw_item


def update_runtime_config(updates: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(updates, dict):
        raise ValueError("config updates must be a JSON object")

    with _runtime_config_lock:
        data = dict(_raw_runtime_config())
        changed: list[str] = []
        for key, raw_item in updates.items():
            field = FIELDS.get(key)
            if field is None or not is_field_editable(field):
                continue

            clear, raw_value = _extract_update_value(raw_item)
            if clear:
                data.pop(key, None)
                changed.append(key)
                continue

            if field.sensitive and raw_value == "":
                continue
            if raw_value is None:
                data.pop(key, None)
                changed.append(key)
                continue

            data[key] = _coerce_value(field, raw_value)
            changed.append(key)

        path = _runtime_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                json.dump(data, tmp, ensure_ascii=False, indent=2)
                tmp.write("\n")
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

        reload_runtime_config()
    sync_bridge_ws_env()
    return {"ok": True, "changed": sorted(set(changed)), "config": get_config_metadata()}


def effective_public_base_url() -> str:
    configured = str(get_config_value("gateway.public_base_url", "") or "").strip().rstrip("/")
    if configured:
        return configured
    host = str(get_config_value("server.host", _default_server_host()))
    port = int(get_config_value("server.port", _default_server_port()))
    return f"http://{host}:{port}"


def effective_ws_url() -> str:
    active_tunnel = os.getenv(ACTIVE_TUNNEL_WS_ENV, "").strip()
    if active_tunnel:
        return active_tunnel
    return str(get_config_value("gateway.ws_tunnel_url", _default_ws_url()) or _default_ws_url())


def sync_bridge_ws_env(ws_url: str | None = None) -> str:
    resolved = (ws_url or effective_ws_url()).strip()
    os.environ[BRIDGE_WS_ENV] = resolved
    return resolved
