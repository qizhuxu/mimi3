"""
config — 两层配置加载。

层序：os.getenv(VAR) > data/config/config.json[section][key] > 默认值。
.env 文件由 uv run --env-file .env 或外部加载到进程环境；os.getenv 直接读到。
data/config/config.json 存非敏感运营参数（gitignored），运行时手动编辑或 WebUI 写回。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

_PROJECT = Path(__file__).resolve().parent.parent  # src/..
CONFIG_DIR = _PROJECT / "data" / "config"
CONFIG_FILE = CONFIG_DIR / "config.json"
LEGACY_CONFIG_FILE = _PROJECT / "config.json"

# 默认值
_DEFAULTS: dict[str, Any] = {
    "pool": {"min_accounts": 8, "max_accounts": 50},
    "scheduler": {
        "tick_seconds": 30,
        "handoff_lead_seconds": 1800,
        "daily_cooldown_seconds": 86400,
        "max_concurrent_deploys": 1,
    },
    "health": {"interval_seconds": 300},
    "deploy": {"send_timeout": 900, "prompt_id": "deploy.v1.standard"},
    "webui": {"history_limit": 10, "host": "0.0.0.0", "port": 8358},
    "tunnel": {
        "public_hostname": "mimo.7786.pp.ua",
        "local_port": 8359,
        "upstream": "api-sgp-oc.xiaomimimo.com:443",
        "api_key_env": "MIMO_API_KEY",
    },
    "prompt_store": {
        "templates_path": "data/prompts/templates.json",
        "substitution_values": {},
    },
}

# 能从 os.getenv 读取的 env → config 键映射（小写下划线：值）
_ENV_KEYS = {
    "MIMI3N_MIN_ACCOUNTS": "pool.min_accounts",
    "MIMI3N_TICK_SECONDS": "scheduler.tick_seconds",
    "MIMI3_WEBUI_PORT": "webui.port",
    "WEBUI_HOST": "webui.host",
    "WEBUI_PORT": "webui.port",
    "CF_API_TOKEN": "cf_api_token",
    "CF_ACCOUNT_ID": "cf_account_id",
    "TUNNEL_TOKEN": "tunnel_token",
    "PROXY_API_KEY": "proxy_api_key",
    "PUBLIC_HOSTNAME": "tunnel.public_hostname",
    "LOCAL_PORT": "tunnel.local_port",
}


def _set_nested(cfg: dict, key_path: str, value: Any) -> None:
    """在嵌套 dict 里设值，如 "scheduler.tick_seconds" → cfg["scheduler"]["tick_seconds"]。"""
    parts = key_path.split(".")
    d = cfg
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = value


def _deep_merge(base: dict, overlay: dict) -> None:
    """递归合并 overlay 到 base。"""
    for k, v in overlay.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def ensure_config_file() -> Path:
    """Return the runtime config path and copy legacy root config on first use."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists() and LEGACY_CONFIG_FILE.exists():
        shutil.copyfile(LEGACY_CONFIG_FILE, CONFIG_FILE)
    return CONFIG_FILE


def load() -> dict[str, Any]:
    """
    加载完整配置。返回 dict：
      - 先取默认值
      - data/config/config.json（如果存在）覆盖
      - os.getenv（来自 .env）再覆盖
    """
    cfg: dict[str, Any] = json.loads(json.dumps(_DEFAULTS))  # deep copy

    # 1. data/config/config.json (legacy root config.json is copied on first use)
    config_path = ensure_config_file()
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                _deep_merge(cfg, raw)
        except Exception:
            pass

    # 2. os.getenv（.env 由 uv run --env-file 加载到进程环境）
    for env_key, cfg_path in _ENV_KEYS.items():
        val = os.getenv(env_key, "").strip()
        if val:
            # 尝试数值类型转换（int / float）
            if val == "true":
                val = True
            elif val == "false":
                val = False
            else:
                try:
                    if "." in val:
                        val = float(val)
                    else:
                        val = int(val)
                except ValueError:
                    pass
            _set_nested(cfg, cfg_path, val)

    return cfg


# ---- 便利访问 ----
_cached: dict[str, Any] | None = None


def settings() -> dict[str, Any]:
    """懒加载缓存后的配置。"""
    global _cached
    if _cached is None:
        _cached = load()
    return _cached


def reload() -> None:
    """重读配置（运行时热加载用）。"""
    global _cached
    _cached = load()


if __name__ == "__main__":
    c = load()
    import json as j
    print(j.dumps(c, ensure_ascii=False, indent=2))
