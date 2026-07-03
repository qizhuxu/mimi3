"""
account_store — 账号池 + per-account 持久化状态。

creds/user_<uid>.json    凭据（Step 2 已有，load_credentials 加载）
state/user_<uid>.state.json  部署状态（本模块管理，原子写）
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

# 部署状态枚举
S_IDLE = "idle"                    # 无实例，等调度
S_NEEDS_DEPLOY = "needs_deploy"    # 实例在但无 skill 记录，待部署
S_DEPLOYING = "deploying"          # 部署中
S_ACTIVE = "active"                # claw 活着（实例+skill）
S_COOLDOWN = "cooldown"            # 24h 冷却中（当天已用过配额）
S_RELOGIN_NEEDED = "relogin_needed"  # cookie 失效，待补号
S_DISABLED = "disabled"            # 已清理移出活池

ACTIVE_LIFETIME = 4 * 3600          # claw 实例寿命 4h


@dataclass
class AccountState:
    uid: str
    deploy_state: str = S_IDLE
    deployed_at: Optional[float] = None
    expires_at: Optional[float] = None
    connector_id: Optional[str] = None
    last_result: Optional[str] = None
    last_error_detail: Optional[str] = None
    consecutive_failures: int = 0
    last_deploy_attempt_at: Optional[float] = None
    cooldown_until: Optional[float] = None      # 短退避（network_error）
    disabled_reason: Optional[str] = None
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AccountState":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})

    def is_eligible_for_deploy(self, now: float, daily_cooldown: float = 86400) -> bool:
        """是否可部署：非 disabled/relogin，且 24h 冷却已过，且无短退避。"""
        if self.deploy_state in (S_DISABLED, S_RELOGIN_NEEDED, S_DEPLOYING):
            return False
        if self.cooldown_until and now < self.cooldown_until:
            return False
        if self.deployed_at and now < self.deployed_at + daily_cooldown:
            return False  # 24h 滚动冷却未过
        return True

    def is_active(self, now: float) -> bool:
        if self.deploy_state != S_ACTIVE:
            return False
        if self.expires_at and now > self.expires_at:
            return False  # 寿命已到（状态还没更新但实际过期）
        return True


class AccountPool:
    """账号池：凭据 + 状态。manager 是唯一写者。"""

    def __init__(self, creds_dir: Path, state_dir: Path, daily_cooldown: float = 86400):
        self.creds_dir = Path(creds_dir)
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.daily_cooldown = daily_cooldown
        self._creds: dict[str, dict] = {}      # uid -> creds dict
        self._states: dict[str, AccountState] = {}
        self.load()

    # ---- 加载 ----
    def load(self) -> None:
        self._creds.clear()
        self._states.clear()
        if not self.creds_dir.exists():
            return
        for p in sorted(self.creds_dir.glob("user_*.json")):
            try:
                from claw_deployer import load_credentials
                creds = load_credentials(p)
            except Exception:
                continue
            uid = str(creds.get("userId", "")).strip()
            if not uid:
                continue
            self._creds[uid] = creds
            self._states[uid] = self._load_state(uid) or AccountState(uid=uid)

    def _load_state(self, uid: str) -> Optional[AccountState]:
        p = self.state_dir / f"user_{uid}.state.json"
        if not p.exists():
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                return AccountState.from_dict(json.load(f))
        except Exception:
            return None

    def save_state(self, state: AccountState) -> None:
        state.updated_at = time.time()
        self._states[state.uid] = state
        p = self.state_dir / f"user_{state.uid}.state.json"
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)  # 原子替换

    # ---- 查询 ----
    def get_creds(self, uid: str) -> Optional[dict]:
        return self._creds.get(uid)

    def get_state(self, uid: str) -> Optional[AccountState]:
        return self._states.get(uid)

    def all_uids(self) -> list[str]:
        return sorted(self._creds.keys())

    def list_active_uids(self) -> list[str]:
        """活池：非 disabled/relogin。"""
        return [u for u in self.all_uids()
                if self._states[u].deploy_state not in (S_DISABLED, S_RELOGIN_NEEDED)]

    def list_in_state(self, state: str) -> list[str]:
        return [u for u in self.all_uids() if self._states[u].deploy_state == state]

    # ---- 操作 ----
    def disable(self, uid: str, reason: str) -> None:
        st = self._states.get(uid)
        if st is None:
            return
        st.deploy_state = S_DISABLED
        st.disabled_reason = reason
        self.save_state(st)

    def enable(self, uid: str) -> None:
        st = self._states.get(uid)
        if st is None:
            return
        st.deploy_state = S_IDLE
        st.disabled_reason = None
        st.consecutive_failures = 0
        self.save_state(st)

    def reload_credentials(self, uid: str) -> bool:
        """操作员补号后重读凭据文件。"""
        p = self.creds_dir / f"user_{uid}.json"
        if not p.exists():
            return False
        try:
            from claw_deployer import load_credentials
            creds = load_credentials(p)
        except Exception:
            return False
        if str(creds.get("userId", "")).strip() != uid:
            return False
        self._creds[uid] = creds
        return True

    def add_creds_file(self, path: Path) -> Optional[str]:
        """导入新凭据文件到 creds/。返回 uid 或 None。"""
        path = Path(path)
        try:
            from claw_deployer import load_credentials
            creds = load_credentials(path)
        except Exception:
            return None
        uid = str(creds.get("userId", "")).strip()
        if not uid:
            return None
        dest = self.creds_dir / f"user_{uid}.json"
        dest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        self._creds[uid] = creds
        if uid not in self._states:
            self._states[uid] = AccountState(uid=uid)
        return uid

    def snapshot(self) -> list[dict]:
        """所有账号状态快照（CLI/status 用）。"""
        now = time.time()
        out = []
        for uid in self.all_uids():
            st = self._states[uid]
            out.append({
                "uid": uid,
                "name": self._creds[uid].get("name", uid),
                "deploy_state": st.deploy_state,
                "deployed_at": st.deployed_at,
                "expires_at": st.expires_at,
                "remain_sec": max(0, int(st.expires_at - now)) if st.expires_at else None,
                "connector_id": st.connector_id,
                "last_result": st.last_result,
                "consecutive_failures": st.consecutive_failures,
                "cooldown_until": st.cooldown_until,
                "eligible": st.is_eligible_for_deploy(now, self.daily_cooldown),
                "disabled_reason": st.disabled_reason,
            })
        return out


if __name__ == "__main__":
    # 冒烟测
    base = Path(__file__).resolve().parent
    pool = AccountPool(base / "creds", base / "state")
    print(f"账号数: {len(pool.all_uids())} / 活池: {len(pool.list_active_uids())}")
    for row in pool.snapshot():
        print(f"  {row['uid']} {row['deploy_state']} remain={row['remain_sec']} eligible={row['eligible']}")
