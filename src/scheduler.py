"""
scheduler — 串行接力调度计划 + 选号。

stagger 由配置固定控制（默认 120min，前端限制 60-180min）。
每个调度点最多给出一个候选账号，失败后的换号由 AccountManager 串行执行。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from account_store import (
    AccountPool, AccountState, ACTIVE_LIFETIME, S_ACTIVE, S_COOLDOWN,
    S_DEPLOYING, S_DISABLED, S_IDLE, S_NEEDS_DEPLOY, S_RELOGIN_NEEDED,
)

MIN_STAGGER = 30 * 60      # 最短错峰 30min（N 很大时也别太密）
MAX_STAGGER = 6 * 3600     # 最长 6h（N=4 时也至少每 6h 一个）


@dataclass
class DeployTask:
    """一项部署任务。reason 说明为什么触发。"""
    uid: str                        # 要部署的账号
    reason: str                     # needs_deploy / handoff / dead_claw / manual
    handoff_from: Optional[str] = None  # handoff 时=即将过期的旧账号 uid


@dataclass
class Plan:
    due_deploys: list[DeployTask] = field(default_factory=list)
    reserve_size: int = 0
    coverage_gap: bool = False
    coverage_risk: bool = False       # stagger > claw 寿命，N 太少相邻有缝
    stagger_interval: float = 0.0
    active_count: int = 0
    eligible_count: int = 0
    now: float = 0.0


class Scheduler:
    def __init__(self, config: dict):
        self.config = config
        self.handoff_lead = config.get("scheduler.handoff_lead_seconds", 1800)
        self.daily_cooldown = config.get("scheduler.daily_cooldown_seconds", 86400)
        self.max_concurrent = config.get("scheduler.max_concurrent_deploys", 1)
        # round-robin 游标：按 all_uids 顺序轮转，优先最久没部署的
        self._rr_index = 0

    def stagger_interval(self, n_active: int) -> float:
        """返回固定错峰间隔（秒），由 config scheduler.stagger_seconds 控制，默认 120 分钟。"""
        return self.config.get("scheduler.stagger_seconds",
                               self.config.get("scheduler_stagger_seconds", 7200))

    def latest_success_at(self, pool: AccountPool) -> Optional[float]:
        """最近一次成功部署时间，用作串行接力的下一次部署锚点。"""
        stamps: list[float] = []
        for uid in pool.all_uids():
            st = pool.get_state(uid)
            if st and st.deployed_at and (
                st.last_result == "success" or st.deploy_state == S_ACTIVE
            ):
                stamps.append(st.deployed_at)
        return max(stamps) if stamps else None

    def _eligible_idle(self, pool: AccountPool, now: float) -> list[str]:
        """可部署的 idle 账号（24h 冷却已过 + 无短退避）。"""
        out = []
        for uid in pool.all_uids():
            st = pool.get_state(uid)
            if st is None:
                continue
            if st.deploy_state in (S_DISABLED, S_RELOGIN_NEEDED, S_DEPLOYING, S_ACTIVE):
                continue
            if st.is_eligible_for_deploy(now, self.daily_cooldown):
                out.append(uid)
        return out

    def compute_plan(self, pool: AccountPool, now: Optional[float] = None) -> Plan:
        now = now if now is not None else time.time()
        active_uids = [u for u in pool.list_active_uids()
                       if (pool.get_state(u) or None) and pool.get_state(u).is_active(now)]
        eligible = self._eligible_idle(pool, now)
        stagger = self.stagger_interval(len(pool.list_active_uids()))

        tasks: list[DeployTask] = []

        # 串行接力：任一调度点最多产生一个候选账号，避免一次性烧完整个队列。
        if len(active_uids) == 0 and eligible:
            pick = self.pick_next_deploy(pool, now)
            if pick:
                tasks.append(DeployTask(uid=pick.uid, reason="bootstrap"))
        elif active_uids and eligible:
            last_success = self.latest_success_at(pool)
            if last_success is not None and now >= last_success + stagger:
                pick = self.pick_next_deploy(pool, now)
                if pick:
                    tasks.append(DeployTask(uid=pick.uid, reason="scheduled"))

        reserve = len(eligible)
        # 覆盖缺口：无 active 且无可接力账号
        coverage_gap = (len(active_uids) == 0 and reserve == 0)
        # 覆盖风险：stagger > claw 寿命 → 相邻 claw 之间有缝（N 太少）
        coverage_risk = stagger > ACTIVE_LIFETIME

        return Plan(
            due_deploys=tasks, reserve_size=reserve,
            coverage_gap=coverage_gap, coverage_risk=coverage_risk,
            stagger_interval=stagger,
            active_count=len(active_uids), eligible_count=reserve, now=now,
        )

    def pick_next_deploy(self, pool: AccountPool, now: Optional[float] = None,
                         exclude: Optional[set[str]] = None) -> Optional[AccountState]:
        """选下一个该部署的 eligible idle 账号。优先 needs_deploy 自身，其次 round-robin 最久没部署的。"""
        now = now if now is not None else time.time()
        exclude = exclude or set()
        eligible = [u for u in self._eligible_idle(pool, now) if u not in exclude]
        if not eligible:
            return None
        # 优先最久没部署的（deployed_at 最小/None）
        eligible.sort(key=lambda u: pool.get_state(u).deployed_at or 0)
        return pool.get_state(eligible[0])

    def assign_handoff_targets(self, pool: AccountPool, plan: Plan, now: Optional[float] = None) -> Plan:
        """给 handoff 类型的 task 填上接班账号 uid。原地改 plan.due_deploys。"""
        now = now if now is not None else time.time()
        used: set[str] = set()
        for t in plan.due_deploys:
            if t.reason == "handoff" and not t.uid:
                # 排除 handoff_from 自己（它正在过期，不能接班自己）
                pick = self.pick_next_deploy(pool, now, exclude=used | {t.handoff_from} if t.handoff_from else used)
                if pick is None:
                    t.uid = ""  # reserve 耗尽，保留空 uid 让 manager 报 gap
                else:
                    t.uid = pick.uid
                    used.add(pick.uid)
        return plan


if __name__ == "__main__":
    # 冒烟测：用真实 creds 池跑 plan
    from pathlib import Path
    base = Path(__file__).resolve().parent
    pool = AccountPool(base / "creds", base / "state")
    sched = Scheduler({})
    plan = sched.compute_plan(pool)
    plan = sched.assign_handoff_targets(pool, plan)
    print(f"active={plan.active_count} eligible={plan.eligible_count} "
          f"stagger={plan.stagger_interval/60:.0f}min reserve={plan.reserve_size} "
          f"gap={plan.coverage_gap}")
    print(f"due_deploys ({len(plan.due_deploys)}):")
    for t in plan.due_deploys:
        print(f"  {t.reason}: uid={t.uid or '(无reserve)'} handoff_from={t.handoff_from}")
