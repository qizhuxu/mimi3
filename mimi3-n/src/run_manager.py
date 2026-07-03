"""
run_manager — 账号管理器 CLI。

用法:
  python run_manager.py run             # 常驻主循环（对账 + 调度 + health + cleanup）
  python run_manager.py plan            # dry-run：打印 pool 状态 + 调度计划，不部署
  python run_manager.py status          # 打印所有账号状态
  python run_manager.py deploy <uid>    # 手动部署指定账号
  python run_manager.py reload <uid>    # 操作员补号后重读凭据 + 回池

环境变量（均可选，未配 L3 降级）:
  CF_API_TOKEN / CF_ACCOUNT_ID / TUNNEL_TOKEN  启用 L3（同 deploy_one.py）
  PUBLIC_HOSTNAME / PROXY_API_KEY              L1 公网探测
  MIMI3N_MIN_ACCOUNTS=8                        最少账号数
  MIMI3N_TICK_SECONDS=30                       主循环节奏
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# 让 Python 找到 src/ 里的同级模块（无论 cwd 在哪）
_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from account_manager import AccountManager
from account_store import AccountPool
from claw_deployer import build_logger
from health_monitor import HealthMonitor
from prompt_store import PromptStore
from scheduler import Scheduler
from tunnel_health import TunnelHealth
from deploy_one import decode_tunnel_id


BASE_DIR = _SRC.parent  # src/.. → 项目根目录（data/creds/ data/state/ data/prompts/ data/logs/）


def _config() -> dict:
    # 用新 config.py 加载（.env + data/config.json 两源合并）
    from config import settings
    c = settings()
    return {
        "pool.min_accounts": c["pool"]["min_accounts"],
        "scheduler.tick_seconds": c["scheduler"]["tick_seconds"],
        "scheduler.handoff_lead_seconds": c["scheduler"]["handoff_lead_seconds"],
        "scheduler.daily_cooldown_seconds": c["scheduler"]["daily_cooldown_seconds"],
        "scheduler.max_concurrent_deploys": c["scheduler"]["max_concurrent_deploys"],
        "health.interval_seconds": c["health"]["interval_seconds"],
        "deploy.send_timeout": c["deploy"]["send_timeout"],
        "deploy.prompt_id": c["deploy"]["prompt_id"],
    }


def _build_manager(config: dict, *, build_tunnel: bool = True):
    logger = build_logger("account-manager")
    pool = AccountPool(BASE_DIR / "data" / "creds", BASE_DIR / "data" / "state",
                       daily_cooldown=config["scheduler.daily_cooldown_seconds"])
    store = PromptStore(BASE_DIR / "data" / "prompts" / "templates.json", logger=logger)
    sched = Scheduler(config)

    # 可选 L3
    tunnel_health = None
    if build_tunnel:
        cf_token = os.getenv("CF_API_TOKEN", "").strip()
        cf_account = os.getenv("CF_ACCOUNT_ID", "").strip()
        tunnel_id = os.getenv("TUNNEL_ID", "").strip()
        if not tunnel_id:
            tt = os.getenv("TUNNEL_TOKEN", "").strip()
            if tt:
                tunnel_id = decode_tunnel_id(tt)
        if cf_token and cf_account and tunnel_id:
            tunnel_health = TunnelHealth(account_id=cf_account, tunnel_id=tunnel_id,
                                         api_token=cf_token, logger=logger)
            logger.info(f"L3 启用：account={cf_account} tunnel={tunnel_id}")
        else:
            logger.info("L3 关闭（无 CF_API_TOKEN/CF_ACCOUNT_ID/TUNNEL_TOKEN），health 降级")

    hm = HealthMonitor(tunnel_health=tunnel_health,
                       interval=config["health.interval_seconds"], logger=logger)
    public_hostname = os.getenv("PUBLIC_HOSTNAME", "").strip() or None
    proxy_api_key = os.getenv("PROXY_API_KEY", "").strip() or None

    mgr = AccountManager(
        pool, sched, hm, store,
        out_dir=BASE_DIR / "data" / "logs",
        config=config, logger=logger,
        tunnel_health=tunnel_health,
        public_hostname=public_hostname,
        proxy_api_key=proxy_api_key,
        send_timeout=config["deploy.send_timeout"],
        max_concurrent_deploys=config["scheduler.max_concurrent_deploys"],
    )
    return mgr


def _print_plan(mgr: AccountManager) -> None:
    import time
    now = time.time()
    print("\n=== 账号池状态 ===")
    for row in mgr.status():
        remain = f"{row['remain_sec']}s" if row["remain_sec"] is not None else "-"
        print(f"  {row['uid']}  state={row['deploy_state']:<14} remain={remain:<8} "
              f"eligible={row['eligible']} connector={row['connector_id'] or '-'} "
              f"fail={row['consecutive_failures']} last={row['last_result'] or '-'}")
    plan = mgr.scheduler.compute_plan(mgr.pool, now)
    plan = mgr.scheduler.assign_handoff_targets(mgr.pool, plan, now)
    print(f"\n=== 调度计划 (now={int(now)}) ===")
    print(f"  active={plan.active_count} eligible={plan.eligible_count} "
          f"stagger={plan.stagger_interval/60:.0f}min reserve={plan.reserve_size} "
          f"gap={plan.coverage_gap} risk={plan.coverage_risk}")
    print(f"  due_deploys ({len(plan.due_deploys)}):")
    for t in plan.due_deploys:
        print(f"    {t.reason}: uid={t.uid or '(无reserve)'} handoff_from={t.handoff_from or '-'}")


async def _cmd_run(mgr: AccountManager) -> int:
    await mgr.run()
    return 0


async def _cmd_plan(mgr: AccountManager) -> int:
    await mgr.reconcile_on_boot()
    _print_plan(mgr)
    return 0


async def _cmd_status(mgr: AccountManager) -> int:
    print("\n=== 账号池状态 ===")
    for row in mgr.status():
        remain = f"{row['remain_sec']}s" if row["remain_sec"] is not None else "-"
        print(f"  {row['uid']}  state={row['deploy_state']:<14} remain={remain:<8} "
              f"connector={row['connector_id'] or '-'} last={row['last_result'] or '-'}")
    return 0


async def _cmd_deploy(mgr: AccountManager, uid: str) -> int:
    result = await mgr.manual_deploy(uid)
    print(f"\n部署结果: success={result.success} error={result.error_type} "
          f"connector={result.connector_id} elapsed={result.elapsed_sec:.1f}s")
    return 0 if result.success else (1 if result.needs_relogin else 2)


async def _cmd_reload(mgr: AccountManager, uid: str) -> int:
    if mgr.reload_account(uid):
        print(f"{uid} 已重读凭据并回池 idle")
        return 0
    print(f"{uid} 重读凭据失败（文件不存在或 uid 不匹配）", file=sys.stderr)
    return 2


async def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    cmd = sys.argv[1]
    config = _config()
    # plan/status 不需要建 tunnel_health（不部署，L3 用不上）
    build_tunnel = cmd in ("run", "deploy")
    mgr = _build_manager(config, build_tunnel=build_tunnel)

    if cmd == "run":
        return await _cmd_run(mgr)
    if cmd == "plan":
        return await _cmd_plan(mgr)
    if cmd == "status":
        return await _cmd_status(mgr)
    if cmd == "deploy":
        if len(sys.argv) < 3:
            print("用法: python run_manager.py deploy <uid>", file=sys.stderr)
            return 2
        return await _cmd_deploy(mgr, sys.argv[2])
    if cmd == "reload":
        if len(sys.argv) < 3:
            print("用法: python run_manager.py reload <uid>", file=sys.stderr)
            return 2
        return await _cmd_reload(mgr, sys.argv[2])
    print(f"未知命令: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
