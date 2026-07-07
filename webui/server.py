"""
webui.server — FastAPI backend for the operator dashboard.

Usage:
  uv run --env-file .env uvicorn webui.server:app --host 127.0.0.1 --port 8358

API routes:
  GET  /api/status               — account pool snapshot + by_state summary
  GET  /api/plan                 — scheduler plan (stagger, coverage, due_deploys)
  GET  /api/history              — deployment and account event timeline
  GET  /api/config               — config dump (secrets masked)
  POST /api/deploy/{uid}         — trigger one-off deploy for an account
  POST /api/account/{uid}/enable — re-enable a disabled account
  POST /api/account/{uid}/disable
  POST /api/account/{uid}/reload-creds
  POST /api/accounts/import      — bulk import JSON credentials or raw cookie text
  DELETE /api/account/{uid}      — delete token-invalid accounts
  POST /api/config/reload
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ── project root + src/ (src modules use bare imports like `from account_store import`) ──
_PROJECT = Path(__file__).resolve().parent.parent  # webui/.. → project root
for p in (str(_PROJECT), str(_PROJECT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from src.config import CONFIG_FILE as _RUNTIME_CONFIG_FILE, ensure_config_file
from src.prompt_store import ensure_prompt_templates_file

# ── FastAPI app ─────────────────────────────────────────────────
app = FastAPI(title="mimi3 运维控制台")

static_dir = _PROJECT / "webui" / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


def _configured_secret(name: str) -> str:
    return os.getenv(name, "").strip() or _read_env_file().get(name, "").strip()


def _auth_cookie_value(password: str) -> str:
    return hashlib.sha256(f"mimi3-webui:{password}".encode("utf-8")).hexdigest()


def _is_authenticated(request: Request) -> bool:
    password = _configured_secret("WEBUI_PASSWORD")
    if not password:
        return True
    cookie = request.cookies.get("mimi3_webui_auth", "")
    return hmac.compare_digest(cookie, _auth_cookie_value(password))


@app.middleware("http")
async def _webui_auth_middleware(request: Request, call_next):
    password = _configured_secret("WEBUI_PASSWORD")
    if not password:
        return await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/") or path.startswith("/api/auth/"):
        return await call_next(request)
    if _is_authenticated(request):
        return await call_next(request)
    return JSONResponse({"detail": "需要登录工作台"}, status_code=401)


# ── singletons ──────────────────────────────────────────────────

@functools.lru_cache(maxsize=1)
def _get_pool():
    from src.account_store import AccountPool
    from src.config import settings as load_config

    cfg = load_config()

    return AccountPool(
        creds_dir=_PROJECT / "data" / "creds",
        state_dir=_PROJECT / "data" / "state",
        daily_cooldown=cfg.get("scheduler", {}).get("daily_cooldown_seconds", 86400),
    )


@functools.lru_cache(maxsize=1)
def _get_scheduler():
    from src.config import settings as load_config
    from src.scheduler import Scheduler

    return Scheduler(load_config())


# ── helpers ─────────────────────────────────────────────────────

def _format_due_deploys(plan) -> list[dict[str, Any]]:
    return [
        {
            "uid": t.uid,
            "reason": t.reason,
            "handoff_from": t.handoff_from,
        }
        for t in plan.due_deploys
    ]


_deploy_locks: dict[str, asyncio.Lock] = {}


def _get_deploy_lock(uid: str) -> asyncio.Lock:
    lock = _deploy_locks.get(uid)
    if lock is None:
        lock = asyncio.Lock()
        _deploy_locks[uid] = lock
    return lock


def _deploy_response(result) -> dict[str, Any]:
    return {
        "uid": result.uid,
        "success": result.success,
        "error_type": result.error_type,
        "error_detail": result.error_detail,
        "needs_relogin": result.needs_relogin,
        "connector_id": result.connector_id,
        "instance_status": result.instance_status,
        "instance_remain_sec": result.instance_remain_sec,
        "elapsed_sec": result.elapsed_sec,
        "attempts": result.attempts,
    }


_scheduler_operation_lock = asyncio.Lock()
_scheduler_loop_task: asyncio.Task | None = None
_scheduler_loop_manager: Any | None = None
_scheduler_state: dict[str, Any] = {
    "running": False,
    "mode": "idle",
    "started_at": None,
    "stopped_at": None,
    "last_tick_at": None,
    "last_tick_result": None,
    "last_error": None,
    "active_operation": None,
    "operation_id": None,
}


def _operation_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}"


def _plan_response(plan) -> dict[str, Any]:
    return {
        "active_count": plan.active_count,
        "eligible_count": plan.eligible_count,
        "reserve_size": plan.reserve_size,
        "coverage_gap": plan.coverage_gap,
        "coverage_risk": plan.coverage_risk,
        "stagger_interval": plan.stagger_interval,
        "due_deploys": _format_due_deploys(plan),
        "timestamp": plan.now,
    }


def _scheduler_loop_running() -> bool:
    return _scheduler_loop_task is not None and not _scheduler_loop_task.done()


def _scheduler_status_payload() -> dict[str, Any]:
    if _scheduler_loop_task is not None and _scheduler_loop_task.done():
        _scheduler_state["running"] = False
        if _scheduler_state.get("mode") not in {"failed", "stopping"}:
            _scheduler_state["mode"] = "idle"

    due_count = 0
    active_count = 0
    max_concurrent = 1
    try:
        from src.config import settings as load_config

        cfg = load_config()
        max_concurrent = int(cfg.get("scheduler", {}).get("max_concurrent_deploys", 1))
        pool = _get_pool()
        pool.load()
        plan = _get_scheduler().assign_handoff_targets(pool, _get_scheduler().compute_plan(pool))
        due_count = len(plan.due_deploys)
        active_count = plan.active_count
    except Exception as e:
        _scheduler_state["last_error"] = str(e)

    return {
        **_scheduler_state,
        "running": _scheduler_loop_running(),
        "due_count": due_count,
        "active_count": active_count,
        "max_concurrent_deploys": max_concurrent,
        "timestamp": time.time(),
    }


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


async def _require_confirm(request: Request) -> dict[str, Any]:
    body = await _json_body(request)
    if body.get("confirm") is not True:
        raise HTTPException(status_code=400, detail="请确认后再执行该操作")
    return body


def _scheduler_busy() -> bool:
    return _scheduler_operation_lock.locked() or bool(_scheduler_state.get("active_operation"))


def _manager_plan(manager) -> dict[str, Any]:
    manager.pool.load()
    plan = manager.scheduler.compute_plan(manager.pool)
    plan = manager.scheduler.assign_handoff_targets(manager.pool, plan)
    return _plan_response(plan)


async def _run_scheduler_loop(manager) -> None:
    original_tick = manager._tick

    async def tracked_tick():
        operation_id = _operation_id("scheduler-loop-tick")
        _scheduler_state.update({
            "active_operation": "scheduler_loop_tick",
            "operation_id": operation_id,
            "mode": "executing",
            "last_error": None,
        })
        try:
            await original_tick()
            _scheduler_state.update({
                "last_tick_at": time.time(),
                "last_tick_result": "success",
                "last_error": None,
            })
        except Exception as e:
            _scheduler_state.update({
                "last_tick_at": time.time(),
                "last_tick_result": "failed",
                "last_error": str(e),
            })
            raise
        finally:
            _scheduler_state["active_operation"] = None
            if _scheduler_state.get("running"):
                _scheduler_state["mode"] = "loop"

    manager._tick = tracked_tick
    try:
        await manager.run()
    except asyncio.CancelledError:
        manager.stop()
        raise
    except Exception as e:
        _scheduler_state.update({"mode": "failed", "last_error": str(e)})
    finally:
        _scheduler_state.update({
            "running": False,
            "active_operation": None,
            "stopped_at": time.time(),
        })
        if _scheduler_state.get("mode") != "failed":
            _scheduler_state["mode"] = "idle"


_CONFIG_FILE = _RUNTIME_CONFIG_FILE
_ENV_FILE = _PROJECT / ".env"
_DEFAULT_PROMPT_TEMPLATES_FILE = _PROJECT / "data" / "prompts" / "templates.json"
_DEFAULT_PROMPT_TEMPLATES_SEED_FILE = _PROJECT / "defaults" / "prompts" / "templates.json"
_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}$")
_ENV_EDIT_KEYS = {
    "WEBUI_PASSWORD",
    "TUNNEL_TOKEN",
    "PUBLIC_HOSTNAME",
    "CF_API_TOKEN",
    "CF_ACCOUNT_ID",
    "PROXY_API_KEY",
}
_IMPORT_REQUIRED_FIELDS = ("userId", "serviceToken", "xiaomichatbot_ph")
_COOKIE_PAIR_RE = re.compile(r"(?:^|[;\r\n]\s*)\s*([A-Za-z0-9_.-]+)\s*=\s*([^;\r\n]*)")


def _read_json_config() -> dict[str, Any]:
    target = ensure_config_file() if _CONFIG_FILE == _RUNTIME_CONFIG_FILE else _CONFIG_FILE
    if not target.exists():
        return {}
    with open(target, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _write_json_config(data: dict[str, Any]) -> None:
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CONFIG_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, _CONFIG_FILE)


def _prompt_templates_file() -> Path:
    cfg = _read_json_config()
    raw = cfg.get("prompt_store", {}).get("templates_path") if isinstance(cfg, dict) else None
    if not raw:
        return _DEFAULT_PROMPT_TEMPLATES_FILE
    path = Path(str(raw))
    return path if path.is_absolute() else _PROJECT / path


def _read_prompt_templates(path: Path | None = None) -> dict[str, Any]:
    target = path or _prompt_templates_file()
    try:
        target = ensure_prompt_templates_file(target, default_path=_DEFAULT_PROMPT_TEMPLATES_SEED_FILE)
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=f"提示词模板文件不存在，且默认模板不可用: {e}")
    with open(target, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("templates"), list):
        raise HTTPException(status_code=500, detail="提示词模板文件格式不正确")
    return data


def _write_prompt_templates(data: dict[str, Any], path: Path | None = None) -> None:
    target = path or _prompt_templates_file()
    try:
        target = ensure_prompt_templates_file(target, default_path=_DEFAULT_PROMPT_TEMPLATES_SEED_FILE)
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=f"提示词模板文件不存在，且默认模板不可用: {e}")
    tmp = target.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, target)


def _prompt_template_view(item: dict[str, Any]) -> dict[str, Any]:
    text = str(item.get("text", ""))
    preferred_after = item.get("preferred_after", [])
    if not isinstance(preferred_after, list):
        preferred_after = []
    return {
        "prompt_id": str(item.get("prompt_id", "")),
        "enabled": bool(item.get("enabled", True)),
        "preferred_after": [str(v) for v in preferred_after],
        "text": text,
        "text_length": len(text),
    }


def _find_prompt_template(data: dict[str, Any], prompt_id: str) -> dict[str, Any]:
    for item in data.get("templates", []):
        if isinstance(item, dict) and str(item.get("prompt_id", "")).strip() == prompt_id:
            return item
    raise HTTPException(status_code=404, detail=f"提示词模板不存在: {prompt_id}")


def _read_env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    if not _ENV_FILE.exists():
        return values
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _write_env_file(updates: dict[str, str | None]) -> None:
    existing = _ENV_FILE.read_text(encoding="utf-8").splitlines() if _ENV_FILE.exists() else []
    seen: set[str] = set()
    lines: list[str] = []
    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            lines.append(line)
            continue
        key, _ = stripped.split("=", 1)
        key = key.strip()
        if key in updates:
            seen.add(key)
            value = updates[key]
            if value is None:
                continue
            lines.append(f"{key}={value}")
        else:
            lines.append(line)
    for key, value in updates.items():
        if key not in seen and value is not None:
            lines.append(f"{key}={value}")
    tmp = _ENV_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    os.replace(tmp, _ENV_FILE)


def _clean_import_value(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    return text.strip()


def _credential_from_mapping(data: dict[str, Any]) -> tuple[dict[str, str] | None, str]:
    uid = _clean_import_value(data.get("userId") or data.get("cUserId"))
    creds = {
        "userId": uid,
        "serviceToken": _clean_import_value(data.get("serviceToken")),
        "xiaomichatbot_ph": _clean_import_value(data.get("xiaomichatbot_ph")),
        "name": _clean_import_value(data.get("name")) or (f"Imported_{uid}" if uid else ""),
    }
    missing = [field for field in _IMPORT_REQUIRED_FIELDS if not creds.get(field)]
    if missing:
        labels = {
            "userId": "userId/cUserId",
            "serviceToken": "serviceToken",
            "xiaomichatbot_ph": "xiaomichatbot_ph",
        }
        return None, "缺少字段：" + "、".join(labels.get(field, field) for field in missing)
    return creds, ""


def _parse_json_import_candidates(text: str) -> list[tuple[int, Any]]:
    stripped = text.strip()
    if not stripped:
        return []
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return [(1, parsed)]
    if isinstance(parsed, list):
        return [(i + 1, item) for i, item in enumerate(parsed)]

    decoder = json.JSONDecoder()
    rows: list[tuple[int, Any]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        start = line.find("{")
        if start < 0:
            continue
        try:
            item, _ = decoder.raw_decode(line[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(item, list):
            rows.extend((line_no, sub_item) for sub_item in item)
        else:
            rows.append((line_no, item))
    return rows


def _parse_cookie_mapping(block: str) -> dict[str, str]:
    text = re.sub(r"^\s*cookie\s*:\s*", "", block.strip(), flags=re.IGNORECASE)
    pairs: dict[str, str] = {}
    for match in _COOKIE_PAIR_RE.finditer(text):
        key = match.group(1).strip()
        value = match.group(2).strip()
        if key:
            pairs[key] = value
    return pairs


def _parse_cookie_import_candidates(text: str) -> list[tuple[int, dict[str, str]]]:
    blocks = [b.strip() for b in re.split(r"(?:\r?\n){2,}", text) if b.strip()]
    if not blocks:
        blocks = [text.strip()] if text.strip() else []
    rows: list[tuple[int, dict[str, str]]] = []
    cursor_line = 1
    for block in blocks:
        mapping = _parse_cookie_mapping(block)
        if mapping:
            rows.append((cursor_line, mapping))
        cursor_line += max(1, block.count("\n") + 1)
    return rows


def _parse_account_import_text(text: str, fmt: str = "auto") -> list[dict[str, Any]]:
    raw = str(text or "").strip()
    if not raw:
        return [{"row": 1, "status": "failed", "message": "请输入账号 JSON 或原始 cookie 文本"}]

    fmt = (fmt or "auto").lower()
    candidates: list[tuple[int, Any]] = []
    if fmt in {"auto", "json"}:
        candidates = _parse_json_import_candidates(raw)
    if not candidates and fmt in {"auto", "cookie", "text"}:
        candidates = _parse_cookie_import_candidates(raw)
    if not candidates:
        return [{"row": 1, "status": "failed", "message": "未识别到账号 JSON 或 cookie 键值"}]

    results: list[dict[str, Any]] = []
    for idx, (row, candidate) in enumerate(candidates, start=1):
        if not isinstance(candidate, dict):
            results.append({"row": row or idx, "status": "failed", "message": "该行不是账号对象"})
            continue
        creds, reason = _credential_from_mapping(candidate)
        if not creds:
            results.append({"row": row or idx, "status": "failed", "message": reason})
            continue
        results.append({
            "row": row or idx,
            "uid": creds["userId"],
            "name": creds.get("name") or creds["userId"],
            "credentials": creds,
            "status": "pending",
            "message": "待导入",
        })
    return results


def _public_import_result(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "row": item.get("row"),
        "uid": item.get("uid"),
        "name": item.get("name"),
        "status": item.get("status"),
        "message": item.get("message"),
    }


def _as_int(value: Any, *, name: str, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{name} 必须是数字")
    if parsed < min_value or parsed > max_value:
        raise HTTPException(status_code=400, detail=f"{name} 必须在 {min_value}-{max_value} 之间")
    return parsed


def _validate_hostname(value: Any) -> str:
    host = str(value or "").strip().lower()
    if not host or not _HOST_RE.match(host):
        raise HTTPException(status_code=400, detail="隧道域名格式不正确")
    return host


def _apply_config_update(payload: dict[str, Any]) -> dict[str, Any]:
    cfg = _read_json_config()
    env_updates: dict[str, str | None] = {}

    form = payload.get("project") if isinstance(payload.get("project"), dict) else payload
    pool = cfg.setdefault("pool", {})
    deploy = cfg.setdefault("deploy", {})
    tunnel = cfg.setdefault("tunnel", {})
    webui = cfg.setdefault("webui", {})
    cfg.setdefault("prompt_store", {})

    if "min_accounts" in form:
        pool["min_accounts"] = _as_int(form["min_accounts"], name="号池最低阈值", min_value=1, max_value=500)
    if "max_accounts" in form:
        pool["max_accounts"] = _as_int(form["max_accounts"], name="号池最大阈值", min_value=1, max_value=1000)
    if int(pool.get("max_accounts", 50)) < int(pool.get("min_accounts", 8)):
        raise HTTPException(status_code=400, detail="号池最大阈值不能小于最低阈值")

    if "public_hostname" in form:
        host = _validate_hostname(form["public_hostname"])
        tunnel["public_hostname"] = host
        env_updates["PUBLIC_HOSTNAME"] = host
    if "local_port" in form:
        port = _as_int(form["local_port"], name="mimo-claw 监听端口", min_value=1, max_value=65535)
        tunnel["local_port"] = port
    if "history_limit" in form:
        webui["history_limit"] = _as_int(form["history_limit"], name="部署历史显示条数", min_value=1, max_value=200)
    if "prompt_id" in form:
        prompt_id = str(form.get("prompt_id") or "").strip()
        if not prompt_id:
            raise HTTPException(status_code=400, detail="部署模板不能为空")
        deploy["prompt_id"] = prompt_id

    for key in _ENV_EDIT_KEYS:
        if form.get(f"clear_{key.lower()}") is True:
            env_updates[key] = None
        elif key in form:
            raw = str(form.get(key) or "").strip()
            if raw:
                env_updates[key] = raw

    password = form.get("WEBUI_PASSWORD")
    if password and len(str(password)) < 6:
        raise HTTPException(status_code=400, detail="工作台登录密码至少 6 位")

    if env_updates:
        _write_env_file(env_updates)
        for key, value in env_updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    _write_json_config(cfg)
    return {"updated_config": sorted(cfg.keys()), "updated_env": sorted(env_updates.keys())}


def _event_severity(result: str | None, state: str | None) -> str:
    if result == "success" or state == "active":
        return "ok"
    if state in {"relogin_needed", "disabled"}:
        return "danger"
    if result or state in {"cooldown", "needs_deploy", "deploying"}:
        return "warning"
    return "info"


def _history_from_pool(pool, *, limit: int = 120) -> dict[str, Any]:
    """Build a WebUI timeline from persisted account state."""
    snapshot = {str(row["uid"]): row for row in pool.snapshot()}
    events: list[dict[str, Any]] = []

    def add_event(
        *,
        uid: str,
        kind: str,
        title: str,
        occurred_at: float | None,
        severity: str,
        detail: str | None = None,
        result: str | None = None,
        state: str | None = None,
    ) -> None:
        if not occurred_at:
            return
        row = snapshot.get(str(uid), {})
        events.append({
            "id": f"{uid}:{kind}:{int(occurred_at)}",
            "uid": str(uid),
            "name": row.get("name") or str(uid),
            "kind": kind,
            "title": title,
            "detail": detail,
            "result": result,
            "state": state or row.get("deploy_state"),
            "severity": severity,
            "occurred_at": occurred_at,
            "connector_id": row.get("connector_id"),
        })

    for uid in pool.all_uids():
        st = pool.get_state(uid)
        if st is None:
            continue
        result = st.last_result
        error_detail = "7001限流" if result == "create_rate_limited" else st.last_error_detail
        deploy_title = "部署成功" if result == "success" else "部署失败"
        if st.last_deploy_attempt_at:
            add_event(
                uid=uid,
                kind="deploy",
                title=deploy_title,
                occurred_at=st.last_deploy_attempt_at,
                severity=_event_severity(result, st.deploy_state),
                detail=error_detail or st.connector_id or "已记录部署尝试",
                result=result,
                state=st.deploy_state,
            )
        elif st.deployed_at:
            add_event(
                uid=uid,
                kind="deploy",
                title=deploy_title if result else "部署记录",
                occurred_at=st.deployed_at,
                severity=_event_severity(result, st.deploy_state),
                detail=error_detail or st.connector_id or "已有部署时间",
                result=result,
                state=st.deploy_state,
            )

        if st.deploy_state == "cooldown":
            add_event(
                uid=uid,
                kind="cooldown",
                title="进入冷却",
                occurred_at=st.deployed_at or st.updated_at,
                severity="warning",
                detail=error_detail or "账号暂不可部署",
                result=result,
                state=st.deploy_state,
            )
        elif st.deploy_state == "relogin_needed":
            add_event(
                uid=uid,
                kind="account",
                title="凭据失效",
                occurred_at=st.updated_at,
                severity="danger",
                detail=st.last_error_detail or "凭据可能失效",
                result=result,
                state=st.deploy_state,
            )
        elif st.deploy_state == "disabled":
            add_event(
                uid=uid,
                kind="account",
                title="账号已禁用",
                occurred_at=st.updated_at,
                severity="danger",
                detail=st.disabled_reason or "调度器将跳过该账号",
                result=result,
                state=st.deploy_state,
            )

    events.sort(key=lambda item: item["occurred_at"], reverse=True)
    events = events[:limit]
    summary = {
        "total": len(events),
        "success": sum(1 for e in events if e.get("result") == "success"),
        "failed": sum(1 for e in events if e.get("result") and e.get("result") != "success"),
        "cooldown": sum(1 for e in events if e.get("state") == "cooldown"),
        "needs_action": sum(1 for e in events if e.get("severity") == "danger"),
        "latest_at": events[0]["occurred_at"] if events else None,
    }
    return {"events": events, "summary": summary, "timestamp": time.time()}


def _configured_history_limit(limit: int | None = None) -> int:
    if limit is not None:
        return max(1, min(int(limit), 300))
    from src.config import settings as load_config

    cfg = load_config()
    raw = cfg.get("webui", {}).get("history_limit", 10)
    return max(1, min(int(raw), 200))


# ── root redirect ───────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse(str(static_dir / "index.html"))


# ── API: status ─────────────────────────────────────────────────

@app.get("/api/auth/status")
async def api_auth_status(request: Request):
    required = bool(_configured_secret("WEBUI_PASSWORD"))
    return {"required": required, "authenticated": (not required) or _is_authenticated(request)}


@app.post("/api/auth/login")
async def api_auth_login(request: Request):
    password = _configured_secret("WEBUI_PASSWORD")
    if not password:
        return {"success": True, "required": False}
    body = await request.json()
    submitted = str(body.get("password", ""))
    if not hmac.compare_digest(submitted, password):
        raise HTTPException(status_code=401, detail="密码不正确")
    response = JSONResponse({"success": True, "required": True})
    response.set_cookie(
        "mimi3_webui_auth",
        _auth_cookie_value(password),
        httponly=True,
        samesite="lax",
        max_age=7 * 24 * 3600,
    )
    return response


@app.post("/api/auth/logout")
async def api_auth_logout():
    response = JSONResponse({"success": True})
    response.delete_cookie("mimi3_webui_auth")
    return response


@app.get("/api/status")
async def api_status():
    pool = _get_pool()
    pool.load()  # refresh from disk (run_manager may have changed state)
    snapshot = pool.snapshot()

    by_state: dict[str, int] = {}
    by_workbench_state: dict[str, int] = {}
    for row in snapshot:
        s = row["deploy_state"]
        by_state[s] = by_state.get(s, 0) + 1
        w = row.get("workbench_state") or s
        by_workbench_state[w] = by_workbench_state.get(w, 0) + 1

    return {
        "snapshot": snapshot,
        "by_state": by_state,
        "by_workbench_state": by_workbench_state,
        "timestamp": time.time(),
    }


# ── API: plan ───────────────────────────────────────────────────

@app.get("/api/plan")
async def api_plan():
    pool = _get_pool()
    pool.load()
    sched = _get_scheduler()
    plan = sched.compute_plan(pool)
    plan = sched.assign_handoff_targets(pool, plan)

    return {
        "active_count": plan.active_count,
        "eligible_count": plan.eligible_count,
        "reserve_size": plan.reserve_size,
        "coverage_gap": plan.coverage_gap,
        "coverage_risk": plan.coverage_risk,
        "stagger_interval": plan.stagger_interval,
        "due_deploys": _format_due_deploys(plan),
        "timestamp": plan.now,
    }


# ── API: config ─────────────────────────────────────────────────

@app.get("/api/scheduler/status")
async def api_scheduler_status():
    return _scheduler_status_payload()


@app.post("/api/scheduler/start")
async def api_scheduler_start(request: Request):
    global _scheduler_loop_manager, _scheduler_loop_task

    await _require_confirm(request)
    if _scheduler_loop_running():
        raise HTTPException(status_code=409, detail={
            "message": "调度循环已经在运行",
            "status": _scheduler_status_payload(),
        })

    from src.run_manager import _build_manager, _config

    cfg = _config()
    manager = _build_manager(cfg, build_tunnel=True)
    started_at = time.time()
    _scheduler_loop_manager = manager
    _scheduler_state.update({
        "running": True,
        "mode": "loop",
        "started_at": started_at,
        "stopped_at": None,
        "last_error": None,
        "active_operation": None,
        "operation_id": _operation_id("scheduler-loop"),
    })
    _scheduler_loop_task = asyncio.create_task(_run_scheduler_loop(manager))
    return {
        "success": True,
        "running": True,
        "pid": None,
        "started_at": started_at,
        "mode": "loop",
        "message": "调度循环已启动",
    }


@app.post("/api/scheduler/stop")
async def api_scheduler_stop(request: Request):
    await _require_confirm(request)
    if not _scheduler_loop_running():
        _scheduler_state.update({"running": False, "mode": "idle", "active_operation": None})
        return {
            "success": True,
            "running": False,
            "stopped_at": time.time(),
            "message": "调度循环未运行",
        }

    _scheduler_state["mode"] = "stopping"
    if _scheduler_loop_manager is not None:
        _scheduler_loop_manager.stop()

    try:
        await asyncio.wait_for(asyncio.shield(_scheduler_loop_task), timeout=5.0)
    except asyncio.TimeoutError:
        return {
            "success": True,
            "running": True,
            "stopped_at": None,
            "message": "调度循环正在停止",
        }

    return {
        "success": True,
        "running": False,
        "stopped_at": _scheduler_state.get("stopped_at") or time.time(),
        "message": "调度循环已停止",
    }


@app.post("/api/scheduler/tick")
async def api_scheduler_tick(request: Request):
    body = await _json_body(request)
    dry_run = bool(body.get("dry_run", False))
    if not dry_run and body.get("confirm") is not True:
        raise HTTPException(status_code=400, detail="请确认后再执行调度")
    if _scheduler_loop_running():
        raise HTTPException(status_code=409, detail="调度循环运行中，不能同时执行单次调度")
    if _scheduler_operation_lock.locked():
        raise HTTPException(status_code=409, detail="已有调度操作正在执行")

    async with _scheduler_operation_lock:
        from src.run_manager import _build_manager, _config

        cfg = _config()
        manager = _build_manager(cfg, build_tunnel=True)
        operation_id = _operation_id("scheduler-tick")
        started_at = time.time()
        _scheduler_state.update({
            "active_operation": "scheduler_tick",
            "operation_id": operation_id,
            "mode": "planning" if dry_run else "executing",
            "last_error": None,
        })
        plan_before = _manager_plan(manager)
        error = None
        try:
            if not dry_run:
                await manager._tick()
                _scheduler_state["last_tick_result"] = "success"
        except Exception as e:
            error = str(e)
            _scheduler_state.update({"last_tick_result": "failed", "last_error": error})
        finally:
            finished_at = time.time()
            _scheduler_state.update({
                "active_operation": None,
                "last_tick_at": finished_at,
                "mode": "failed" if error else "idle",
            })

        return {
            "success": error is None,
            "operation_id": operation_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "plan_before": plan_before,
            "results": [],
            "error": error,
        }


@app.post("/api/scheduler/deploy-due")
async def api_scheduler_deploy_due(request: Request):
    body = await _require_confirm(request)
    if _scheduler_loop_running():
        raise HTTPException(status_code=409, detail="调度循环运行中，不能同时执行待部署队列")
    if _scheduler_operation_lock.locked():
        raise HTTPException(status_code=409, detail="已有调度操作正在执行")

    requested = body.get("uids")
    requested_uids = {str(uid) for uid in requested} if isinstance(requested, list) else None
    async with _scheduler_operation_lock:
        from src.run_manager import _build_manager, _config

        cfg = _config()
        manager = _build_manager(cfg, build_tunnel=True)
        manager.pool.load()
        plan = manager.scheduler.compute_plan(manager.pool)
        plan = manager.scheduler.assign_handoff_targets(manager.pool, plan)
        operation_id = _operation_id("deploy-due")
        started_at = time.time()
        timeout = float(cfg.get("deploy.send_timeout", 900)) + 30.0
        results: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        error = None
        _scheduler_state.update({
            "active_operation": "deploy_due",
            "operation_id": operation_id,
            "mode": "executing",
            "last_error": None,
        })
        try:
            for task in plan.due_deploys:
                if not task.uid:
                    skipped.append({
                        "uid": None,
                        "reason": task.reason,
                        "handoff_from": task.handoff_from,
                        "message": "没有可用接班账号",
                    })
                    continue
                if requested_uids is not None and task.uid not in requested_uids:
                    skipped.append({"uid": task.uid, "reason": task.reason, "message": "未选择"})
                    continue
                lock = _get_deploy_lock(task.uid)
                if lock.locked():
                    skipped.append({"uid": task.uid, "reason": task.reason, "message": "账号正在部署中"})
                    continue
                async with lock:
                    try:
                        result = await asyncio.wait_for(
                            manager._execute_deploy(task.uid, task.reason),
                            timeout=timeout,
                        )
                        payload = _deploy_response(result)
                        payload["reason"] = task.reason
                        payload["handoff_from"] = task.handoff_from
                        results.append(payload)
                    except asyncio.TimeoutError:
                        results.append({
                            "uid": task.uid,
                            "reason": task.reason,
                            "handoff_from": task.handoff_from,
                            "success": False,
                            "error_type": "timeout",
                            "error_detail": f"部署超时 {int(timeout)} 秒",
                        })
        except Exception as e:
            error = str(e)
            _scheduler_state["last_error"] = error
        finally:
            finished_at = time.time()
            _scheduler_state.update({
                "active_operation": None,
                "last_tick_at": finished_at,
                "last_tick_result": "failed" if error else "success",
                "mode": "failed" if error else "idle",
            })

    return {
        "success": error is None,
        "operation_id": operation_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "results": results,
        "skipped": skipped,
        "error": error,
    }


@app.get("/api/history")
async def api_history(limit: int | None = None):
    pool = _get_pool()
    pool.load()
    return _history_from_pool(pool, limit=_configured_history_limit(limit))


@app.get("/api/config")
async def api_config():
    from src.config import settings as load_config

    cfg = load_config()
    env_file = _read_env_file()

    def configured(name: str) -> bool:
        return bool(os.getenv(name, "").strip() or env_file.get(name, "").strip())

    # Expose operational params but mask actual secret values
    safe: dict[str, Any] = {
        "pool": cfg.get("pool", {}),
        "scheduler": cfg.get("scheduler", {}),
        "health": cfg.get("health", {}),
        "deploy": cfg.get("deploy", {}),
        "tunnel": cfg.get("tunnel", {}),
        "prompt_store": cfg.get("prompt_store", {}),
        "webui": cfg.get("webui", {}),
        "webui_password_configured": configured("WEBUI_PASSWORD"),
        "cf_api_token_configured": configured("CF_API_TOKEN"),
        "cf_account_id_configured": configured("CF_ACCOUNT_ID"),
        "tunnel_token_configured": configured("TUNNEL_TOKEN"),
        "proxy_api_key_configured": configured("PROXY_API_KEY"),
    }
    return safe


@app.get("/api/prompt-templates")
async def api_prompt_templates():
    cfg = _read_json_config()
    current_id = str(cfg.get("deploy", {}).get("prompt_id", "") or "")
    data = _read_prompt_templates()
    templates = [
        _prompt_template_view(item)
        for item in data.get("templates", [])
        if isinstance(item, dict) and str(item.get("prompt_id", "")).strip()
    ]
    if not current_id and templates:
        current_id = templates[0]["prompt_id"]
    return {"current_prompt_id": current_id, "templates": templates}


@app.post("/api/prompt-templates/{prompt_id}")
async def api_prompt_template_update(prompt_id: str, request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="提示词模板内容必须是对象")
    text = str(body.get("text", ""))
    if not text.strip():
        raise HTTPException(status_code=400, detail="提示词模板正文不能为空")

    data = _read_prompt_templates()
    item = _find_prompt_template(data, prompt_id)
    item["text"] = text
    _write_prompt_templates(data)

    from src.config import reload as reload_config
    reload_config()
    _get_pool.cache_clear()
    _get_scheduler.cache_clear()
    return {"success": True, "template": _prompt_template_view(item)}


@app.post("/api/config/update")
async def api_config_update(request: Request):
    from src.config import reload as reload_config

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="配置内容必须是对象")
    updated = _apply_config_update(body)
    reload_config()
    _get_pool.cache_clear()
    _get_scheduler.cache_clear()
    return {"success": True, **updated}


# ── API: deploy ─────────────────────────────────────────────────

@app.post("/api/deploy/{uid}")
async def api_deploy(uid: str):
    pool = _get_pool()
    pool.load()
    creds = pool.get_creds(uid)
    if not creds:
        raise HTTPException(status_code=404, detail=f"账号 {uid} 不存在")

    if _scheduler_busy():
        raise HTTPException(status_code=409, detail="调度操作正在执行，暂不能手动部署")

    lock = _get_deploy_lock(uid)
    if lock.locked():
        raise HTTPException(status_code=409, detail=f"账号 {uid} 正在部署中")

    async with lock:
        from src.account_store import S_IDLE
        from src.claw_deployer import DeployResult
        from src.run_manager import _build_manager, _config

        cfg = _config()
        manager = _build_manager(cfg, build_tunnel=True)
        timeout = float(cfg.get("deploy.send_timeout", 900)) + 30.0

        try:
            result = await asyncio.wait_for(manager.manual_deploy(uid), timeout=timeout)
        except asyncio.TimeoutError:
            pool.load()
            st = pool.get_state(uid)
            if st:
                st.deploy_state = S_IDLE
                st.last_result = "timeout"
                st.last_error_detail = f"部署超时 {int(timeout)} 秒"
                st.cooldown_until = time.time() + 60
                pool.save_state(st)
            result = DeployResult(
                success=False,
                uid=uid,
                error_type="timeout",
                error_detail=f"部署超时 {int(timeout)} 秒",
            )

        pool.load()
        return _deploy_response(result)


# ── API: account ops ────────────────────────────────────────────

@app.post("/api/account/{uid}/enable")
async def api_account_enable(uid: str):
    pool = _get_pool()
    pool.load()
    state = pool.get_state(uid)
    if state is None:
        raise HTTPException(status_code=404, detail=f"账号 {uid} 不存在")
    pool.enable(uid)
    return {"success": True}


@app.post("/api/account/{uid}/disable")
async def api_account_disable(uid: str, request: Request):
    pool = _get_pool()
    pool.load()
    state = pool.get_state(uid)
    if state is None:
        raise HTTPException(status_code=404, detail=f"账号 {uid} 不存在")

    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    reason = body.get("reason", "操作员在网页控制台禁用")
    pool.disable(uid, reason)
    return {"success": True}


@app.post("/api/accounts/import")
async def api_accounts_import(request: Request):
    body = await _json_body(request)
    text = str(body.get("text") or "")
    fmt = str(body.get("format") or "auto")
    parsed = _parse_account_import_text(text, fmt)

    pool = _get_pool()
    pool.load()
    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    for item in parsed:
        if item.get("status") == "failed":
            results.append(_public_import_result(item))
            continue
        uid = str(item.get("uid") or "")
        if not uid:
            results.append({
                "row": item.get("row"),
                "status": "failed",
                "message": "缺少账号 UID",
            })
            continue
        if uid in seen:
            results.append({
                "row": item.get("row"),
                "uid": uid,
                "name": item.get("name"),
                "status": "skipped",
                "message": "同一批导入中 UID 重复，已跳过",
            })
            continue
        seen.add(uid)
        imported_uid = pool.add_credentials(item.get("credentials") or {})
        if imported_uid:
            results.append({
                "row": item.get("row"),
                "uid": imported_uid,
                "name": item.get("name") or imported_uid,
                "status": "imported",
                "message": "账号已导入并回到空闲池",
            })
        else:
            results.append({
                "row": item.get("row"),
                "uid": uid,
                "name": item.get("name"),
                "status": "failed",
                "message": "凭据写入失败，请检查字段是否完整",
            })

    imported = sum(1 for item in results if item.get("status") == "imported")
    skipped = sum(1 for item in results if item.get("status") == "skipped")
    failed = sum(1 for item in results if item.get("status") == "failed")
    return {
        "success": failed == 0 and imported > 0,
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
        "results": results,
    }


@app.delete("/api/account/{uid}")
async def api_account_delete(uid: str):
    pool = _get_pool()
    pool.load()
    state = pool.get_state(uid)
    if state is None:
        raise HTTPException(status_code=404, detail=f"账号 {uid} 不存在")
    if state.deploy_state in {"active", "deploying"}:
        raise HTTPException(status_code=409, detail="账号正在运行或部署中，不能删除")

    row = next((item for item in pool.snapshot() if str(item.get("uid")) == str(uid)), None)
    token_invalid = bool(row and row.get("workbench_state") == "token_invalid")
    if not token_invalid:
        raise HTTPException(status_code=409, detail="仅允许删除 token 失效账号")
    deleted = pool.delete_account(uid)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"账号 {uid} 不存在")
    return {"success": True, "uid": uid, "message": "账号已删除"}


@app.post("/api/account/{uid}/reload-creds")
async def api_account_reload_creds(uid: str):
    pool = _get_pool()
    pool.load()
    ok = pool.reload_credentials(uid)
    if ok:
        pool.enable(uid)
    return {"success": ok, "found": ok}


# ── API: config reload ──────────────────────────────────────────

@app.post("/api/config/reload")
async def api_config_reload():
    from src.config import reload as reload_config

    reload_config()
    # Also invalidate cached runtime objects so they pick up new config.
    _get_pool.cache_clear()
    _get_scheduler.cache_clear()
    return {"success": True, "message": "配置已重载"}
