"""
webui.server — FastAPI backend for the operator dashboard.

Usage:
  uv run --env-file .env uvicorn webui.server:app --host 127.0.0.1 --port 8358

API routes:
  GET  /api/status               — account pool snapshot + by_state summary
  GET  /api/plan                 — scheduler plan (stagger, coverage, due_deploys)
  GET  /api/config               — config dump (secrets masked)
  POST /api/deploy/{uid}         — trigger one-off deploy for an account
  POST /api/account/{uid}/enable — re-enable a disabled account
  POST /api/account/{uid}/disable
  POST /api/account/{uid}/reload-creds
  POST /api/config/reload
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

# ── project root + src/ (src modules use bare imports like `from account_store import`) ──
_PROJECT = Path(__file__).resolve().parent.parent  # webui/.. → project root
for p in (str(_PROJECT), str(_PROJECT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

# ── FastAPI app ─────────────────────────────────────────────────
app = FastAPI(title="mimi3 Operator Dashboard")

static_dir = _PROJECT / "webui" / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ── singletons ──────────────────────────────────────────────────

@functools.lru_cache(maxsize=1)
def _get_pool():
    from src.account_store import AccountPool

    return AccountPool(
        creds_dir=_PROJECT / "data" / "creds",
        state_dir=_PROJECT / "data" / "state",
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


# ── root redirect ───────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse(str(static_dir / "index.html"))


# ── API: status ─────────────────────────────────────────────────

@app.get("/api/status")
async def api_status():
    pool = _get_pool()
    pool.load()  # refresh from disk (run_manager may have changed state)
    snapshot = pool.snapshot()

    by_state: dict[str, int] = {}
    for row in snapshot:
        s = row["deploy_state"]
        by_state[s] = by_state.get(s, 0) + 1

    return {
        "snapshot": snapshot,
        "by_state": by_state,
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

@app.get("/api/config")
async def api_config():
    from src.config import settings as load_config

    cfg = load_config()

    # Expose operational params but mask actual secret values
    safe: dict[str, Any] = {
        "pool": cfg.get("pool", {}),
        "scheduler": cfg.get("scheduler", {}),
        "health": cfg.get("health", {}),
        "deploy": cfg.get("deploy", {}),
        "cf_api_token_configured": bool(os.getenv("CF_API_TOKEN", "")),
        "cf_account_id_configured": bool(os.getenv("CF_ACCOUNT_ID", "")),
        "tunnel_token_configured": bool(os.getenv("TUNNEL_TOKEN", "")),
        "proxy_api_key_configured": bool(os.getenv("PROXY_API_KEY", "")),
    }
    return safe


# ── API: deploy ─────────────────────────────────────────────────

@app.post("/api/deploy/{uid}")
async def api_deploy(uid: str):
    from src.claw_client import safe_claw_trace_text

    pool = _get_pool()
    pool.load()
    creds = pool.get_creds(uid)
    if not creds:
        raise HTTPException(status_code=404, detail=f"账号 {uid} 不存在")

    logger = logging.getLogger(f"webui-deploy-{uid}")
    logger.setLevel(logging.DEBUG)

    from src.claw_deployer import ClawDeployer, build_logger
    from src.config import settings as load_config
    from src.deploy_one import decode_tunnel_id
    from src.prompt_store import PromptStore
    from src.tunnel_health import TunnelHealth

    logger = build_logger(f"webui-deploy-{uid}")
    cfg = load_config()

    # PromptStore
    templates_path = _PROJECT / cfg.get("prompt_store", {}).get(
        "templates_path", "data/prompts/templates.json"
    )
    store = PromptStore(templates_path, logger=logger)

    # L3 (same setup as deploy_one.py)
    tunnel_health = None
    cf_token = os.getenv("CF_API_TOKEN", "").strip()
    cf_account = os.getenv("CF_ACCOUNT_ID", "").strip()
    tunnel_id = os.getenv("TUNNEL_ID", "").strip()
    if not tunnel_id:
        tt = os.getenv("TUNNEL_TOKEN", "").strip()
        if tt:
            tunnel_id = decode_tunnel_id(tt)

    if cf_token and cf_account and tunnel_id:
        tunnel_health = TunnelHealth(
            account_id=cf_account, tunnel_id=tunnel_id,
            api_token=cf_token, logger=logger,
        )
        ok = await tunnel_health.probe()
        if not ok:
            logger.warning("L3 probe 失败，降级 L1+L2")
            tunnel_health = None

    public_hostname = os.getenv("PUBLIC_HOSTNAME", "").strip() or None
    proxy_api_key = os.getenv("PROXY_API_KEY", "").strip() or None

    deployer = ClawDeployer(
        creds, logger,
        prompt_store=store,
        out_dir=_PROJECT / "data" / "logs",
        tunnel_health=tunnel_health,
        public_hostname=public_hostname,
        proxy_api_key=proxy_api_key,
    )

    prompt_id = cfg.get("deploy", {}).get("prompt_id", "deploy.v1.standard")

    try:
        result = await asyncio.wait_for(deployer.deploy(prompt_id), timeout=120.0)
    except asyncio.TimeoutError:
        return {
            "uid": uid,
            "success": False,
            "error_type": "timeout",
            "error_detail": "部署超时 120 秒",
        }  # fmt: skip

    # Refresh pool state after deploy
    pool.load()

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
    reason = body.get("reason", "operator disabled via WebUI")
    pool.disable(uid, reason)
    return {"success": True}


@app.post("/api/account/{uid}/reload-creds")
async def api_account_reload_creds(uid: str):
    pool = _get_pool()
    pool.load()
    ok = pool.reload_credentials(uid)
    return {"success": ok, "found": ok}


# ── API: config reload ──────────────────────────────────────────

@app.post("/api/config/reload")
async def api_config_reload():
    from src.config import reload as reload_config

    reload_config()
    # Also invalidate the cached scheduler so it picks up new config
    _get_scheduler.cache_clear()
    return {"success": True, "message": "config reloaded"}