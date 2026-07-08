"""
account_manager — 账号管理编排器。

启动对账（probe + 持久化 state 分流）→ 主循环（health 检查 + due 部署 + cleanup）
+ manual_deploy（手动指定账号部署）+ cleanup_expired（disable auth_expired 账号）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

from account_store import (
    AccountPool, AccountState, S_ACTIVE, S_COOLDOWN, S_DEPLOYING,
    S_DISABLED, S_IDLE, S_NEEDS_DEPLOY, S_RELOGIN_NEEDED, ACTIVE_LIFETIME,
)
from claw_deployer import ClawDeployer, DeployResult, build_logger, credentials_to_client_params
from deploy_errors import (
    AUTH_EXPIRED, CREATE_FAILED, CREATE_PEAK_RATE_LIMITED, CREATE_RATE_LIMITED, DEPLOY_REFUSED,
    NETWORK_ERROR, VERIFY_FAILED, needs_relogin,
)
from health_monitor import HealthMonitor, probe_status
from prompt_store import PromptStore
from scheduler import DeployTask, Plan, Scheduler
from tunnel_health import TunnelHealth


class AccountManager:
    def __init__(
        self,
        pool: AccountPool,
        scheduler: Scheduler,
        health_monitor: HealthMonitor,
        prompt_store: PromptStore,
        *,
        out_dir: Path,
        config: dict,
        logger: Optional[logging.Logger] = None,
        tunnel_health: Optional[TunnelHealth] = None,
        public_hostname: Optional[str] = None,
        proxy_api_key: Optional[str] = None,
        send_timeout: int = 900,
        max_concurrent_deploys: int = 1,
    ):
        self.pool = pool
        self.scheduler = scheduler
        self.health = health_monitor
        self.prompt_store = prompt_store
        self.out_dir = Path(out_dir)
        self.config = config
        self.logger = logger or build_logger("account-manager")
        self.tunnel_health = tunnel_health
        self.public_hostname = public_hostname
        self.proxy_api_key = proxy_api_key
        self.send_timeout = send_timeout
        self._semaphore = asyncio.Semaphore(max_concurrent_deploys)
        self._cancelled = False
        self._stop_event = asyncio.Event()

    # ---------------- 启动对账 ----------------
    async def reconcile_on_boot(self) -> None:
        """每账号探云端实例 + 读 state → 设 deploy_state。"""
        self.logger.info("=== 启动对账 ===")
        now = time.time()
        for uid in self.pool.all_uids():
            st = self.pool.get_state(uid)
            if st is None:
                continue
            if st.deploy_state in (S_DISABLED, S_RELOGIN_NEEDED):
                self.logger.info(f"[{uid}] 跳过（{st.deploy_state}）")
                continue
            creds = self.pool.get_creds(uid)
            if not creds:
                continue
            _, cookies, _ = credentials_to_client_params(creds)
            cloud_st, remain, http = await probe_status(cookies, self.logger)
            if http == 401:
                st.deploy_state = S_RELOGIN_NEEDED
                st.last_result = AUTH_EXPIRED
                self.logger.warning(f"[{uid}] cookie 失效（401）→ relogin_needed")
            elif cloud_st == "AVAILABLE" and remain > 0:
                # 实例在——看 state 有无 skill 部署记录
                if (st.deployed_at and (now - st.deployed_at < ACTIVE_LIFETIME)
                        and st.connector_id):
                    st.deploy_state = S_ACTIVE
                    st.expires_at = now + remain
                    self.logger.info(f"[{uid}] active（实例+skill 已部署，remain {remain}s）")
                else:
                    st.deploy_state = S_NEEDS_DEPLOY
                    self.logger.info(f"[{uid}] needs_deploy（实例在但无 skill 记录，remain {remain}s）")
            else:
                st.deploy_state = S_IDLE
                self.logger.info(f"[{uid}] idle（无实例，status={cloud_st!r}）")
            self.pool.save_state(st)
        # 池子规模 + L3 降级提示
        n_active_pool = len(self.pool.list_active_uids())
        min_accts = self.config.get("pool.min_accounts", 8)
        if n_active_pool < min_accts:
            self.logger.warning(
                f"⚠ 账号池太小：{n_active_pool} < min {min_accts}，错峰 stagger 过大、相邻 claw 有缝"
            )
        if not self.health.l3_enabled() and n_active_pool >= min_accts:
            self.logger.warning(
                "⚠ health L3 未启用（无 CF_API_TOKEN），cloudflared 崩溃检出延迟可达一个调度周期"
            )

    # ---------------- 主循环 ----------------
    async def run(self) -> None:
        self._cancelled = False
        self._stop_event.clear()
        await self.reconcile_on_boot()
        tick = self.config.get("scheduler.tick_seconds", 30)
        self.logger.info(f"=== 主循环启动 tick={tick}s ===")
        while not self._cancelled:
            try:
                recovered = await self._tick()
                if not recovered:
                    await self._run_serial_schedule_step()
            except Exception as e:
                self.logger.exception(f"主循环 tick 异常: {e}")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=tick)
            except asyncio.TimeoutError:
                pass
        self.logger.info("主循环已退出")

    def stop(self) -> None:
        self._cancelled = True
        self._stop_event.set()

    async def _tick(self) -> bool:
        """执行一次心跳检查；如发现隧道失活，立即接力补位。

        普通按间隔接力由 `_run_serial_schedule_step()` 推进，避免“tick”
        被当成批量调度入口。
        """
        now = time.time()
        health_recovery_needed = False
        # 1. 健康检查 active 账号
        for uid in self.pool.list_in_state(S_ACTIVE):
            st = self.pool.get_state(uid)
            creds = self.pool.get_creds(uid)
            if not (st and creds):
                continue
            # 先看寿命：expires_at 过了→转 idle（实例已到期）
            if st.expires_at and now > st.expires_at:
                st.deploy_state = S_IDLE
                st.expires_at = None
                self.logger.info(f"[{uid}] 实例寿命到期 → idle")
                self.pool.save_state(st)
                continue
            if not self.health.should_check(uid, now):
                continue
            _, cookies, _ = credentials_to_client_params(creds)
            alive, detail = await self.health.check(st, cookies)
            if not alive:
                # 实例还在但 claw 死（cloudflared 崩）/ cookie 失效
                if "401" in detail:
                    st.deploy_state = S_RELOGIN_NEEDED
                    st.last_result = AUTH_EXPIRED
                else:
                    st.deploy_state = S_COOLDOWN  # 等下个调度点补位
                    st.consecutive_failures += 1
                    health_recovery_needed = True
                self.logger.warning(f"[{uid}] health 不活: {detail} → {st.deploy_state}")
                self.pool.save_state(st)

        if health_recovery_needed:
            await self._deploy_until_success("health_recovery", first_uid=None)

        self.cleanup_expired()
        return health_recovery_needed

    async def _run_serial_schedule_step(self) -> list[DeployResult]:
        """按串行接力计划推进一次：最多从一个候选开始，失败才换下一个。"""
        now = time.time()
        plan = self.scheduler.compute_plan(self.pool, now)
        plan = self.scheduler.assign_handoff_targets(self.pool, plan, now)
        if plan.coverage_gap:
            self.logger.error(f"⚠ 覆盖缺口！active=0 due={len(plan.due_deploys)} reserve=0")
        if plan.coverage_risk:
            self.logger.warning(
                f"⚠ 覆盖风险：stagger={plan.stagger_interval/60:.0f}min > claw 寿命 "
                f"{ACTIVE_LIFETIME//60}min，相邻 claw 有缝（N={plan.active_count} 太少）"
            )

        if not plan.due_deploys:
            return []

        task = plan.due_deploys[0]
        if not task.uid:
            self.logger.error(f"handoff_from={task.handoff_from} 无 reserve 账号可接班，断档")
            return []
        return await self._deploy_until_success(task.reason, first_uid=task.uid)

    # ---------------- 部署执行 ----------------
    def _pick_next_serial_uid(
        self,
        now: float,
        attempted: set[str],
        allowed_uids: Optional[set[str]] = None,
    ) -> Optional[str]:
        if allowed_uids is None:
            pick = self.scheduler.pick_next_deploy(self.pool, now, exclude=attempted)
            return pick.uid if pick else None

        candidates: list[str] = []
        for uid in sorted(allowed_uids):
            if uid in attempted:
                continue
            st = self.pool.get_state(uid)
            if st is None:
                continue
            if st.deploy_state in (S_DISABLED, S_RELOGIN_NEEDED, S_DEPLOYING, S_ACTIVE):
                continue
            if st.is_eligible_for_deploy(now, self.scheduler.daily_cooldown):
                candidates.append(uid)
        if not candidates:
            return None
        candidates.sort(key=lambda u: self.pool.get_state(u).deployed_at or 0)
        return candidates[0]

    async def _deploy_until_success(
        self,
        reason: str,
        *,
        first_uid: Optional[str] = None,
        allowed_uids: Optional[set[str]] = None,
    ) -> list[DeployResult]:
        """串行尝试账号，直到第一个部署成功或候选池耗尽。"""
        results: list[DeployResult] = []
        attempted: set[str] = set()
        while not self._cancelled:
            now = time.time()
            uid = None
            if first_uid and first_uid not in attempted and (
                allowed_uids is None or first_uid in allowed_uids
            ):
                uid = first_uid
            if uid is None:
                uid = self._pick_next_serial_uid(now, attempted, allowed_uids)
            if not uid:
                self.logger.warning(f"串行部署无可用账号 reason={reason} attempted={len(attempted)}")
                break

            attempted.add(uid)
            async with self._semaphore:
                result = await self._execute_deploy(uid, reason)
            results.append(result)
            if result.success:
                self.logger.info(f"串行部署成功 uid={uid} reason={reason} attempts={len(results)}")
                break
            self.cleanup_expired()
            self.logger.warning(
                f"串行部署失败，尝试下一个账号 uid={uid} error={result.error_type} "
                f"attempts={len(results)}"
            )
        return results

    async def _execute_deploy(self, uid: str, reason: str) -> DeployResult:
        creds = self.pool.get_creds(uid)
        if not creds:
            self.logger.error(f"[{uid}] 无凭据")
            return DeployResult(success=False, uid=uid, error_type="no_credentials")
        st = self.pool.get_state(uid)
        if st:
            st.deploy_state = S_DEPLOYING
            st.last_deploy_attempt_at = time.time()
            self.pool.save_state(st)
        self.logger.info(f"[{uid}] 开始部署 reason={reason}")
        deployer = ClawDeployer(
            creds, self.logger,
            prompt_store=self.prompt_store,
            out_dir=self.out_dir,
            send_timeout=self.send_timeout,
            tunnel_health=self.tunnel_health,
            public_hostname=self.public_hostname,
            proxy_api_key=self.proxy_api_key,
        )
        result = await deployer.deploy(self.config.get("deploy.prompt_id", "deploy.v1.standard"))
        await self._apply_result(uid, result)
        return result

    async def _apply_result(self, uid: str, result: DeployResult) -> None:
        st = self.pool.get_state(uid)
        if st is None:
            return
        now = time.time()
        st.last_result = result.error_type if not result.success else "success"
        st.last_error_detail = result.error_detail
        if result.success:
            st.deploy_state = S_ACTIVE
            st.deployed_at = now
            st.expires_at = now + ACTIVE_LIFETIME
            st.connector_id = result.connector_id
            st.consecutive_failures = 0
            st.cooldown_until = None
            self.logger.info(f"[{uid}] 部署成功 active connector={result.connector_id}")
        else:
            et = result.error_type
            if needs_relogin(et):  # auth_expired
                st.deploy_state = S_RELOGIN_NEEDED
                self.logger.warning(f"[{uid}] cookie 失效 → relogin_needed")
            elif et in (CREATE_FAILED, CREATE_RATE_LIMITED):
                # 当天花配额 → 24h 冷却起算
                st.deploy_state = S_COOLDOWN
                st.deployed_at = now  # 冷却从这次尝试起算
                if et == CREATE_RATE_LIMITED:
                    st.last_error_detail = "7001限流"
                self.logger.warning(f"[{uid}] {et} → 24h 冷却")
            elif et == CREATE_PEAK_RATE_LIMITED:
                # 高峰限流不代表 24h 创建额度已消耗，回到下一个调度周期。
                st.deploy_state = S_IDLE
                st.deployed_at = None
                st.expires_at = None
                st.connector_id = None
                st.cooldown_until = None
                st.last_error_detail = "高峰限流"
                self.logger.warning(f"[{uid}] {et} → 下个调度周期重试")
            elif et == NETWORK_ERROR:
                # 没创建成功不算花配额，短退避后可重试
                st.deploy_state = S_IDLE
                st.cooldown_until = now + 60  # 1min 退避
                self.logger.warning(f"[{uid}] network_error → 1min 退避")
            elif et in (DEPLOY_REFUSED, VERIFY_FAILED):
                # ClawDeployer 内已换模板仍败 → 算花配额 + 24h 冷却
                st.deploy_state = S_COOLDOWN
                st.deployed_at = now
                st.consecutive_failures += 1
                self.logger.warning(f"[{uid}] {et} → 24h 冷却（配额已花）")
            else:
                st.deploy_state = S_IDLE
                st.cooldown_until = now + 30
        self.pool.save_state(st)

    # ---------------- 手动操作 ----------------
    async def manual_deploy(self, uid: str) -> DeployResult:
        """绕过调度，直接部署指定账号。"""
        return await self._execute_deploy(uid, reason="manual")

    def cleanup_expired(self) -> None:
        """token 失效账号 → disable 移出活池。"""
        for uid in self.pool.list_in_state(S_RELOGIN_NEEDED):
            self.pool.disable(uid, "auth_expired")
            self.logger.warning(f"[{uid}] disabled（auth_expired，待操作员补号）")

    def reload_account(self, uid: str) -> bool:
        """操作员补号后重读凭据 + enable。"""
        if not self.pool.reload_credentials(uid):
            return False
        self.pool.enable(uid)
        self.logger.info(f"[{uid}] 已重读凭据并回池 idle")
        return True

    def status(self) -> list[dict]:
        return self.pool.snapshot()
