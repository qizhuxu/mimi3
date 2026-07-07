import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from webui import server
from src.account_store import AccountPool, AccountState, S_ACTIVE, S_RELOGIN_NEEDED
from src.claw_deployer import DeployResult


def reset_scheduler_runtime():
    server._scheduler_loop_task = None
    server._scheduler_loop_manager = None
    server._scheduler_state.update({
        "running": False,
        "mode": "idle",
        "started_at": None,
        "stopped_at": None,
        "last_tick_at": None,
        "last_tick_result": None,
        "last_error": None,
        "active_operation": None,
        "operation_id": None,
    })


class WebUIServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_scheduler_runtime()

    async def test_api_deploy_uses_account_manager_and_returns_result(self):
        pool = MagicMock()
        pool.get_creds.return_value = {"userId": "u1"}
        manager = MagicMock()
        manager.manual_deploy = AsyncMock(
            return_value=DeployResult(
                success=True,
                uid="u1",
                connector_id="connector-1",
                elapsed_sec=1.2,
                attempts=1,
            )
        )

        server._deploy_locks.clear()
        with patch.object(server, "_get_pool", return_value=pool), \
             patch("src.run_manager._config", return_value={"deploy.send_timeout": 1}), \
             patch("src.run_manager._build_manager", return_value=manager):
            response = await server.api_deploy("u1")

        manager.manual_deploy.assert_awaited_once_with("u1")
        self.assertTrue(response["success"])
        self.assertEqual(response["uid"], "u1")
        self.assertEqual(response["connector_id"], "connector-1")
        pool.load.assert_called()

    async def test_reload_creds_reenables_account_when_found(self):
        pool = MagicMock()
        pool.reload_credentials.return_value = True

        with patch.object(server, "_get_pool", return_value=pool):
            response = await server.api_account_reload_creds("u1")

        self.assertEqual(response, {"success": True, "found": True})
        pool.reload_credentials.assert_called_once_with("u1")
        pool.enable.assert_called_once_with("u1")

    async def test_api_status_includes_workbench_state_summary(self):
        pool = MagicMock()
        pool.snapshot.return_value = [
            {"uid": "u1", "deploy_state": "active", "workbench_state": "running"},
            {"uid": "u2", "deploy_state": "cooldown", "workbench_state": "cooldown", "last_result": "create_rate_limited"},
            {"uid": "u3", "deploy_state": "relogin_needed", "workbench_state": "token_invalid"},
        ]

        with patch.object(server, "_get_pool", return_value=pool):
            response = await server.api_status()

        self.assertEqual(response["by_state"]["active"], 1)
        self.assertEqual(response["by_workbench_state"]["running"], 1)
        self.assertEqual(response["by_workbench_state"]["cooldown"], 1)
        self.assertEqual(response["by_workbench_state"]["token_invalid"], 1)

    def test_import_parser_accepts_json_and_raw_cookie_text(self):
        parsed_json = server._parse_account_import_text(json.dumps({
            "userId": "u-json",
            "serviceToken": "json-service-token",
            "xiaomichatbot_ph": "json-ph",
            "name": "JSON 账号",
        }, ensure_ascii=False))
        parsed_cookie = server._parse_account_import_text(
            "deviceId=d1; cUserId=u-cookie; serviceToken=cookie-service-token; "
            "xiaomichatbot_ph=cookie-ph; passInfo=ok"
        )

        self.assertEqual(parsed_json[0]["uid"], "u-json")
        self.assertEqual(parsed_json[0]["credentials"]["serviceToken"], "json-service-token")
        self.assertEqual(parsed_cookie[0]["uid"], "u-cookie")
        self.assertEqual(parsed_cookie[0]["credentials"]["xiaomichatbot_ph"], "cookie-ph")

    def test_import_parser_reports_missing_cookie_fields_without_secret_echo(self):
        parsed = server._parse_account_import_text(
            "cUserId=u-bad; serviceToken=secret-service-token; passInfo=ok"
        )

        self.assertEqual(parsed[0]["status"], "failed")
        self.assertIn("xiaomichatbot_ph", parsed[0]["message"])
        self.assertNotIn("secret-service-token", json.dumps(parsed, ensure_ascii=False))

    async def test_api_accounts_import_writes_creds_and_does_not_echo_tokens(self):
        with tempfile.TemporaryDirectory() as td:
            pool = AccountPool(Path(td) / "creds", Path(td) / "state")
            request = MagicMock()
            request.json = AsyncMock(return_value={
                "format": "auto",
                "text": (
                    "deviceId=d1; cUserId=u-cookie; serviceToken=secret-service-token; "
                    "xiaomichatbot_ph=secret-ph; passInfo=ok"
                ),
            })

            with patch.object(server, "_get_pool", return_value=pool):
                response = await server.api_accounts_import(request)

            saved = json.loads((Path(td) / "creds" / "user_u-cookie.json").read_text(encoding="utf-8"))
            public_text = json.dumps(response, ensure_ascii=False)
            self.assertTrue(response["success"])
            self.assertEqual(response["imported"], 1)
            self.assertEqual(saved["serviceToken"], "secret-service-token")
            self.assertNotIn("secret-service-token", public_text)
            self.assertNotIn("secret-ph", public_text)

    async def test_api_accounts_import_skips_duplicate_uid_in_same_payload(self):
        with tempfile.TemporaryDirectory() as td:
            pool = AccountPool(Path(td) / "creds", Path(td) / "state")
            request = MagicMock()
            request.json = AsyncMock(return_value={
                "format": "json",
                "text": "\n".join([
                    json.dumps({"userId": "dup", "serviceToken": "t1", "xiaomichatbot_ph": "p1"}),
                    json.dumps({"userId": "dup", "serviceToken": "t2", "xiaomichatbot_ph": "p2"}),
                ]),
            })

            with patch.object(server, "_get_pool", return_value=pool):
                response = await server.api_accounts_import(request)

            self.assertEqual(response["imported"], 1)
            self.assertEqual(response["skipped"], 1)
            self.assertEqual(response["results"][1]["status"], "skipped")

    async def test_api_account_delete_removes_token_invalid_account_files(self):
        with tempfile.TemporaryDirectory() as td:
            pool = AccountPool(Path(td) / "creds", Path(td) / "state")
            pool.add_credentials({"userId": "expired", "serviceToken": "token", "xiaomichatbot_ph": "ph"})
            state = pool.get_state("expired")
            state.deploy_state = S_RELOGIN_NEEDED
            pool.save_state(state)

            with patch.object(server, "_get_pool", return_value=pool):
                response = await server.api_account_delete("expired")

            self.assertTrue(response["success"])
            self.assertFalse((Path(td) / "creds" / "user_expired.json").exists())
            self.assertFalse((Path(td) / "state" / "user_expired.state.json").exists())

    async def test_api_account_delete_rejects_active_and_unknown_accounts(self):
        with tempfile.TemporaryDirectory() as td:
            pool = AccountPool(Path(td) / "creds", Path(td) / "state")
            pool.add_credentials({"userId": "active", "serviceToken": "token", "xiaomichatbot_ph": "ph"})
            state = pool.get_state("active")
            state.deploy_state = S_ACTIVE
            state.expires_at = 9999999999.0
            pool.save_state(state)

            with patch.object(server, "_get_pool", return_value=pool):
                with self.assertRaises(Exception) as active_ctx:
                    await server.api_account_delete("active")
                with self.assertRaises(Exception) as missing_ctx:
                    await server.api_account_delete("missing")

            self.assertEqual(active_ctx.exception.status_code, 409)
            self.assertEqual(missing_ctx.exception.status_code, 404)

    def test_history_from_pool_sorts_events_and_summarizes(self):
        pool = MagicMock()
        pool.snapshot.return_value = [
            {"uid": "u1", "name": "账号 1", "deploy_state": "cooldown", "connector_id": None},
            {"uid": "u2", "name": "账号 2", "deploy_state": "active", "connector_id": "conn-2"},
        ]
        pool.all_uids.return_value = ["u1", "u2"]
        states = {
            "u1": SimpleNamespace(
                uid="u1",
                deploy_state="cooldown",
                deployed_at=20.0,
                connector_id=None,
                last_result="create_failed",
                last_error_detail="实例终态: CREATE_FAILED",
                last_deploy_attempt_at=20.0,
                disabled_reason=None,
                updated_at=21.0,
            ),
            "u2": SimpleNamespace(
                uid="u2",
                deploy_state="active",
                deployed_at=10.0,
                connector_id="conn-2",
                last_result="success",
                last_error_detail=None,
                last_deploy_attempt_at=10.0,
                disabled_reason=None,
                updated_at=11.0,
            ),
        }
        pool.get_state.side_effect = lambda uid: states[uid]

        history = server._history_from_pool(pool)

        self.assertEqual(history["events"][0]["uid"], "u1")
        self.assertEqual(history["events"][0]["title"], "部署失败")
        self.assertEqual(history["events"][0]["severity"], "warning")
        self.assertEqual(history["summary"]["success"], 1)
        self.assertEqual(history["summary"]["failed"], 2)
        self.assertEqual(history["summary"]["cooldown"], 2)

    def test_history_from_pool_honors_limit(self):
        pool = MagicMock()
        uids = [f"u{i:02d}" for i in range(12)]
        pool.snapshot.return_value = [
            {"uid": uid, "name": uid, "deploy_state": "idle", "connector_id": None}
            for uid in uids
        ]
        pool.all_uids.return_value = uids
        states = {
            uid: SimpleNamespace(
                uid=uid,
                deploy_state="idle",
                deployed_at=None,
                connector_id=None,
                last_result="success",
                last_error_detail=None,
                last_deploy_attempt_at=float(1000 + idx),
                disabled_reason=None,
                updated_at=float(1000 + idx),
            )
            for idx, uid in enumerate(uids)
        }
        pool.get_state.side_effect = lambda uid: states[uid]

        history = server._history_from_pool(pool, limit=10)

        self.assertEqual(len(history["events"]), 10)
        self.assertEqual(history["events"][0]["uid"], "u11")
        self.assertEqual(history["events"][-1]["uid"], "u02")

    async def test_api_history_refreshes_pool(self):
        pool = MagicMock()
        pool.snapshot.return_value = []
        pool.all_uids.return_value = []

        with patch.object(server, "_get_pool", return_value=pool):
            response = await server.api_history()

        pool.load.assert_called_once()
        self.assertEqual(response["events"], [])
        self.assertEqual(response["summary"]["total"], 0)

    async def test_api_history_uses_configured_default_limit(self):
        pool = MagicMock()
        uids = [f"u{i:02d}" for i in range(12)]
        pool.snapshot.return_value = [
            {"uid": uid, "name": uid, "deploy_state": "idle", "connector_id": None}
            for uid in uids
        ]
        pool.all_uids.return_value = uids
        states = {
            uid: SimpleNamespace(
                uid=uid,
                deploy_state="idle",
                deployed_at=None,
                connector_id=None,
                last_result="success",
                last_error_detail=None,
                last_deploy_attempt_at=float(1000 + idx),
                disabled_reason=None,
                updated_at=float(1000 + idx),
            )
            for idx, uid in enumerate(uids)
        }
        pool.get_state.side_effect = lambda uid: states[uid]

        with patch.object(server, "_get_pool", return_value=pool), \
             patch("src.config.settings", return_value={"webui": {"history_limit": 10}}):
            response = await server.api_history()

        self.assertEqual(len(response["events"]), 10)
        self.assertEqual(response["events"][0]["uid"], "u11")

    async def test_api_history_query_limit_overrides_config_default(self):
        pool = MagicMock()
        pool.snapshot.return_value = [
            {"uid": "u1", "name": "u1", "deploy_state": "idle", "connector_id": None},
            {"uid": "u2", "name": "u2", "deploy_state": "idle", "connector_id": None},
        ]
        pool.all_uids.return_value = ["u1", "u2"]
        states = {
            "u1": SimpleNamespace(
                uid="u1",
                deploy_state="idle",
                deployed_at=None,
                connector_id=None,
                last_result="success",
                last_error_detail=None,
                last_deploy_attempt_at=1000.0,
                disabled_reason=None,
                updated_at=1000.0,
            ),
            "u2": SimpleNamespace(
                uid="u2",
                deploy_state="idle",
                deployed_at=None,
                connector_id=None,
                last_result="success",
                last_error_detail=None,
                last_deploy_attempt_at=1001.0,
                disabled_reason=None,
                updated_at=1001.0,
            ),
        }
        pool.get_state.side_effect = lambda uid: states[uid]

        with patch.object(server, "_get_pool", return_value=pool), \
             patch("src.config.settings", return_value={"webui": {"history_limit": 10}}):
            response = await server.api_history(limit=1)

        self.assertEqual(len(response["events"]), 1)
        self.assertEqual(response["events"][0]["uid"], "u2")

    async def test_prompt_templates_api_reads_and_updates_template_text(self):
        with tempfile.TemporaryDirectory() as td:
            template_path = Path(td) / "templates.json"
            template_path.write_text(json.dumps({
                "templates": [
                    {
                        "prompt_id": "deploy.v1.standard",
                        "enabled": True,
                        "text": "原始提示词 {{PUBLIC_HOSTNAME}}",
                        "preferred_after": [],
                    }
                ]
            }, ensure_ascii=False), encoding="utf-8")
            config_path = Path(td) / "config.json"
            config_path.write_text(json.dumps({
                "deploy": {"prompt_id": "deploy.v1.standard"},
                "prompt_store": {"templates_path": str(template_path)},
            }), encoding="utf-8")
            request = MagicMock()
            request.json = AsyncMock(return_value={"text": "新的提示词正文"})

            with patch.object(server, "_CONFIG_FILE", config_path), \
                 patch.object(server, "_DEFAULT_PROMPT_TEMPLATES_FILE", template_path), \
                 patch("src.config.reload"):
                listing = await server.api_prompt_templates()
                updated = await server.api_prompt_template_update("deploy.v1.standard", request)

            saved = json.loads(template_path.read_text(encoding="utf-8"))
            self.assertEqual(listing["current_prompt_id"], "deploy.v1.standard")
            self.assertEqual(listing["templates"][0]["text"], "原始提示词 {{PUBLIC_HOSTNAME}}")
            self.assertTrue(updated["success"])
            self.assertEqual(updated["template"]["text"], "新的提示词正文")
            self.assertEqual(saved["templates"][0]["text"], "新的提示词正文")

    def test_apply_config_update_writes_project_config_and_env(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=False):
            config_path = Path(td) / "config.json"
            env_path = Path(td) / ".env"
            config_path.write_text(json.dumps({
                "pool": {"min_accounts": 8, "max_accounts": 50},
                "deploy": {"prompt_id": "deploy.v1.standard"},
                "tunnel": {"public_hostname": "old.example.com", "local_port": 8359},
                "prompt_store": {"substitution_values": {}},
            }), encoding="utf-8")
            env_path.write_text("TUNNEL_TOKEN=old-token\n", encoding="utf-8")

            with patch.object(server, "_CONFIG_FILE", config_path), patch.object(server, "_ENV_FILE", env_path):
                result = server._apply_config_update({
                    "project": {
                        "min_accounts": 12,
                        "prompt_id": "deploy.v2.fast",
                        "public_hostname": "mimo.example.com",
                        "local_port": 8361,
                        "history_limit": 25,
                        "WEBUI_PASSWORD": "secret123",
                        "TUNNEL_TOKEN": "new-token",
                        "CF_API_TOKEN": "cf-token",
                    }
                })

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            env_text = env_path.read_text(encoding="utf-8")
            self.assertEqual(saved["pool"]["min_accounts"], 12)
            self.assertEqual(saved["deploy"]["prompt_id"], "deploy.v2.fast")
            self.assertEqual(saved["tunnel"]["public_hostname"], "mimo.example.com")
            self.assertEqual(saved["tunnel"]["local_port"], 8361)
            self.assertEqual(saved["webui"]["history_limit"], 25)
            self.assertNotIn("PUBLIC_HOSTNAME", saved["prompt_store"]["substitution_values"])
            self.assertNotIn("LOCAL_PORT", saved["prompt_store"]["substitution_values"])
            self.assertIn("WEBUI_PASSWORD=secret123", env_text)
            self.assertIn("TUNNEL_TOKEN=new-token", env_text)
            self.assertIn("CF_API_TOKEN=cf-token", env_text)
            self.assertIn("WEBUI_PASSWORD", result["updated_env"])

    def test_apply_config_update_rejects_invalid_values(self):
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "config.json"
            env_path = Path(td) / ".env"
            config_path.write_text("{}", encoding="utf-8")
            env_path.write_text("", encoding="utf-8")

            with patch.object(server, "_CONFIG_FILE", config_path), patch.object(server, "_ENV_FILE", env_path):
                with self.assertRaises(Exception):
                    server._apply_config_update({"project": {"public_hostname": "not a host"}})
                with self.assertRaises(Exception):
                    server._apply_config_update({"project": {"history_limit": 0}})

    def test_write_json_config_creates_data_config_directory(self):
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "data" / "config" / "config.json"
            with patch.object(server, "_CONFIG_FILE", config_path):
                server._write_json_config({"webui": {"history_limit": 18}})

            self.assertEqual(
                json.loads(config_path.read_text(encoding="utf-8"))["webui"]["history_limit"],
                18,
            )

    async def test_auth_status_requires_login_when_password_configured(self):
        request = MagicMock()
        request.cookies = {}
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / ".env"
            env_path.write_text("WEBUI_PASSWORD=secret123\n", encoding="utf-8")
            with patch.object(server, "_ENV_FILE", env_path), patch.dict(os.environ, {}, clear=True):
                response = await server.api_auth_status(request)

        self.assertEqual(response, {"required": True, "authenticated": False})

    async def test_auth_login_sets_cookie_for_correct_password(self):
        request = MagicMock()
        request.json = AsyncMock(return_value={"password": "secret123"})
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / ".env"
            env_path.write_text("WEBUI_PASSWORD=secret123\n", encoding="utf-8")
            with patch.object(server, "_ENV_FILE", env_path), patch.dict(os.environ, {}, clear=True):
                response = await server.api_auth_login(request)

        self.assertEqual(response.status_code, 200)
        self.assertIn("mimi3_webui_auth", response.headers["set-cookie"])

    async def test_auth_logout_deletes_cookie(self):
        response = await server.api_auth_logout()

        self.assertEqual(response.status_code, 200)
        self.assertIn("mimi3_webui_auth", response.headers["set-cookie"])
        self.assertIn("Max-Age=0", response.headers["set-cookie"])

    async def test_scheduler_status_returns_idle_shape(self):
        pool = MagicMock()
        pool.load.return_value = None
        plan = SimpleNamespace(due_deploys=[], active_count=0)
        scheduler = MagicMock()
        scheduler.compute_plan.return_value = plan
        scheduler.assign_handoff_targets.return_value = plan

        with patch.object(server, "_get_pool", return_value=pool), \
             patch.object(server, "_get_scheduler", return_value=scheduler), \
             patch("src.config.settings", return_value={"scheduler": {"max_concurrent_deploys": 2}}):
            response = await server.api_scheduler_status()

        self.assertFalse(response["running"])
        self.assertEqual(response["mode"], "idle")
        self.assertEqual(response["due_count"], 0)
        self.assertEqual(response["max_concurrent_deploys"], 2)

    async def test_scheduler_start_requires_confirm(self):
        request = MagicMock()
        request.json = AsyncMock(return_value={})

        with self.assertRaises(Exception) as ctx:
            await server.api_scheduler_start(request)

        self.assertEqual(ctx.exception.status_code, 400)

    async def test_scheduler_start_rejects_duplicate_loop(self):
        request = MagicMock()
        request.json = AsyncMock(return_value={"confirm": True})
        task = MagicMock()
        task.done.return_value = False
        server._scheduler_loop_task = task

        with self.assertRaises(Exception) as ctx:
            await server.api_scheduler_start(request)

        self.assertEqual(ctx.exception.status_code, 409)

    async def test_scheduler_stop_returns_success_when_not_running(self):
        request = MagicMock()
        request.json = AsyncMock(return_value={"confirm": True})

        response = await server.api_scheduler_stop(request)

        self.assertTrue(response["success"])
        self.assertFalse(response["running"])

    async def test_scheduler_tick_calls_account_manager_once(self):
        request = MagicMock()
        request.json = AsyncMock(return_value={"confirm": True})
        plan = SimpleNamespace(
            active_count=1,
            eligible_count=2,
            reserve_size=2,
            coverage_gap=False,
            coverage_risk=False,
            stagger_interval=3600,
            due_deploys=[],
            now=123.0,
        )
        manager = MagicMock()
        manager.pool.load.return_value = None
        manager.scheduler.compute_plan.return_value = plan
        manager.scheduler.assign_handoff_targets.return_value = plan
        manager._tick = AsyncMock()

        with patch("src.run_manager._config", return_value={"deploy.send_timeout": 1}), \
             patch("src.run_manager._build_manager", return_value=manager):
            response = await server.api_scheduler_tick(request)

        manager._tick.assert_awaited_once()
        self.assertTrue(response["success"])
        self.assertEqual(response["plan_before"]["active_count"], 1)

    async def test_scheduler_deploy_due_requires_confirm(self):
        request = MagicMock()
        request.json = AsyncMock(return_value={})

        with self.assertRaises(Exception) as ctx:
            await server.api_scheduler_deploy_due(request)

        self.assertEqual(ctx.exception.status_code, 400)

    async def test_scheduler_deploy_due_recomputes_plan_and_returns_results(self):
        request = MagicMock()
        request.json = AsyncMock(return_value={"confirm": True})
        task = SimpleNamespace(uid="u1", reason="bootstrap", handoff_from=None)
        plan = SimpleNamespace(due_deploys=[task])
        manager = MagicMock()
        manager.pool.load.return_value = None
        manager.scheduler.compute_plan.return_value = plan
        manager.scheduler.assign_handoff_targets.return_value = plan
        manager._execute_deploy = AsyncMock(return_value=DeployResult(
            success=True,
            uid="u1",
            connector_id="connector-1",
            elapsed_sec=2.0,
        ))
        server._deploy_locks.clear()

        with patch("src.run_manager._config", return_value={"deploy.send_timeout": 1}), \
             patch("src.run_manager._build_manager", return_value=manager):
            response = await server.api_scheduler_deploy_due(request)

        manager.scheduler.compute_plan.assert_called_once_with(manager.pool)
        manager._execute_deploy.assert_awaited_once_with("u1", "bootstrap")
        self.assertTrue(response["success"])
        self.assertEqual(response["results"][0]["uid"], "u1")
        self.assertEqual(response["results"][0]["connector_id"], "connector-1")


if __name__ == "__main__":
    unittest.main()
