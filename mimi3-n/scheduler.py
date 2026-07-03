"""
scheduler — 错峰调度计划 + 选号。

stagger = 24h / N（N=8→3h、N=12→2h），每账号 4h 活跃、24h 滚动冷却。
reserve = 周期内未用且不在冷却的账号；周期末 reserve→0，失败=断档告警。
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
        n = max(n_active, 1)
        s = 24 * 3600 / n
        return max(MIN_STAGGER, min(MAX_STAGGER, s))

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

        # 1. needs_deploy（boot：实例在但无 skill 记录）→ 部署该账号自身
        for uid in pool.list_in_state(S_NEEDS_DEPLOY):
            tasks.append(DeployTask(uid=uid, reason="needs_deploy"))

        # 2. handoff：active 且 expires_at - now < handoff_lead → 提前交接
        for uid in active_uids:
            st = pool.get_state(uid)
            if st is None or st.expires_at is None:
                continue
            if st.expires_at - now < self.handoff_lead:
                # 需要一个新账号接班（下面 pick_next_deploy 选）
                tasks.append(DeployTask(uid="", reason="handoff", handoff_from=uid))

        # 3. dead_claw 由 health_monitor 标 cooldown 后，这里会通过 needs_deploy/handoff 路径补位
        #    （cooldown 账号不进 eligible，自然需要别的号）

        # 4. bootstrap：active==0 且有 eligible → 部署一个启动覆盖（冷启动）
        if len(active_uids) == 0 and eligible:
            pick = self.pick_next_deploy(pool, now)
            if pick:
                tasks.append(DeployTask(uid=pick.uid, reason="bootstrap"))

        reserve = len(eligible)
        # 覆盖缺口：无 active 且有 due 任务但 reserve=0
        coverage_gap = (len(active_uids) == 0 and len(tasks) > 0 and reserve == 0)
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
