import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from account_manager import AccountManager
from account_store import AccountPool, S_COOLDOWN, S_IDLE
from claw_deployer import DeployResult
from deploy_errors import CREATE_PEAK_RATE_LIMITED, CREATE_RATE_LIMITED


class AccountErrorStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_rate_limited_is_persisted_as_7001_cooldown(self):
        with tempfile.TemporaryDirectory() as td:
            pool = AccountPool(Path(td) / "creds", Path(td) / "state")
            pool.add_credentials({
                "userId": "u7001",
                "serviceToken": "token",
                "xiaomichatbot_ph": "ph",
            })
            manager = AccountManager.__new__(AccountManager)
            manager.pool = pool
            manager.logger = logging.getLogger("test-account-error-states")

            await manager._apply_result(
                "u7001",
                DeployResult(
                    success=False,
                    uid="u7001",
                    error_type=CREATE_RATE_LIMITED,
                    error_detail="create code=7001 限流: 今日额度已用完",
                ),
            )

            state = pool.get_state("u7001")
            self.assertEqual(state.deploy_state, S_COOLDOWN)
            self.assertEqual(state.last_result, CREATE_RATE_LIMITED)
            self.assertEqual(state.last_error_detail, "7001限流")
            self.assertFalse(state.is_eligible_for_deploy(state.deployed_at + 1))

            row = pool.snapshot()[0]
            self.assertEqual(row["workbench_state"], "cooldown")
            self.assertEqual(row["workbench_state_label"], "冷却中")
            self.assertEqual(row["last_error_detail"], "7001限流")
            self.assertIsNone(row["retry_after_sec"])
            self.assertGreater(row["cooldown_remaining_sec"], 0)

    async def test_peak_rate_limited_returns_to_idle_for_next_scheduler_cycle(self):
        with tempfile.TemporaryDirectory() as td:
            pool = AccountPool(Path(td) / "creds", Path(td) / "state")
            pool.add_credentials({
                "userId": "u-peak",
                "serviceToken": "token",
                "xiaomichatbot_ph": "ph",
            })
            manager = AccountManager.__new__(AccountManager)
            manager.pool = pool
            manager.logger = logging.getLogger("test-account-error-states")

            await manager._apply_result(
                "u-peak",
                DeployResult(
                    success=False,
                    uid="u-peak",
                    error_type=CREATE_PEAK_RATE_LIMITED,
                    error_detail="当前 Claw 实例负载过高",
                ),
            )

            state = pool.get_state("u-peak")
            self.assertEqual(state.deploy_state, S_IDLE)
            self.assertEqual(state.last_result, CREATE_PEAK_RATE_LIMITED)
            self.assertEqual(state.last_error_detail, "高峰限流")
            self.assertIsNone(state.deployed_at)
            self.assertIsNone(state.cooldown_until)
            self.assertTrue(state.is_eligible_for_deploy(state.updated_at + 1))

            row = pool.snapshot()[0]
            self.assertEqual(row["workbench_state"], "idle")
            self.assertEqual(row["workbench_state_label"], "空闲中")
            self.assertEqual(row["last_error_detail"], "高峰限流")
            self.assertEqual(row["cooldown_remaining_sec"], 0)
            self.assertIsNone(row["retry_after_sec"])

    def test_idle_state_with_recent_7001_attempt_uses_attempt_cooldown(self):
        with tempfile.TemporaryDirectory() as td:
            pool = AccountPool(Path(td) / "creds", Path(td) / "state")
            pool.add_credentials({
                "userId": "u-recent-7001",
                "serviceToken": "token",
                "xiaomichatbot_ph": "ph",
            })
            state = pool.get_state("u-recent-7001")
            now = time.time()
            old_deploy = now - (3 * 86400)
            recent_attempt = now - 60
            state.deploy_state = S_IDLE
            state.deployed_at = old_deploy
            state.last_deploy_attempt_at = recent_attempt
            state.last_result = CREATE_RATE_LIMITED
            state.last_error_detail = "create code=7001 限流: raw upstream detail"
            pool.save_state(state)

            row = pool.snapshot()[0]

            self.assertEqual(row["deploy_state"], S_IDLE)
            self.assertEqual(row["workbench_state"], "cooldown")
            self.assertEqual(row["last_result"], CREATE_RATE_LIMITED)
            self.assertEqual(row["last_error_detail"], "7001限流")
            self.assertGreater(row["cooldown_remaining_sec"], 0)
            self.assertFalse(state.is_eligible_for_deploy(now, pool.daily_cooldown))

    def test_idle_state_with_expired_7001_last_result_returns_to_idle_display(self):
        with tempfile.TemporaryDirectory() as td:
            pool = AccountPool(Path(td) / "creds", Path(td) / "state")
            pool.add_credentials({
                "userId": "u-expired-7001",
                "serviceToken": "token",
                "xiaomichatbot_ph": "ph",
            })
            state = pool.get_state("u-expired-7001")
            old_attempt = time.time() - (3 * 86400)
            state.deploy_state = S_IDLE
            state.deployed_at = old_attempt
            state.last_deploy_attempt_at = old_attempt
            state.last_result = CREATE_RATE_LIMITED
            state.last_error_detail = "create code=7001 限流: raw upstream detail"
            pool.save_state(state)

            row = pool.snapshot()[0]

            self.assertEqual(row["deploy_state"], S_IDLE)
            self.assertEqual(row["workbench_state"], "idle")
            self.assertEqual(row["last_result"], CREATE_RATE_LIMITED)
            self.assertEqual(row["last_error_detail"], "7001限流")
            self.assertEqual(row["cooldown_remaining_sec"], 0)
            self.assertTrue(state.is_eligible_for_deploy(time.time(), pool.daily_cooldown))


if __name__ == "__main__":
    unittest.main()
