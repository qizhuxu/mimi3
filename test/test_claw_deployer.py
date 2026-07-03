import asyncio
import io
import json
import logging
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from claw_client import safe_claw_trace_text
from claw_deployer import ClawDeployer, DeployError, RecordingClawClient
from deploy_errors import SEND_TIMEOUT, VERIFY_FAILED, WS_DISCONNECTED


class _PromptStore:
    def get(self, prompt_id: str):
        return SimpleNamespace(prompt_id=prompt_id, text="deploy prompt")


class _Client:
    async def close(self):
        return None


class ClawDeployerTests(unittest.IsolatedAsyncioTestCase):
    async def test_verify_reply_requires_connector_id_when_l3_enabled(self):
        deployer = ClawDeployer.__new__(ClawDeployer)
        deployer._tunnel_health = object()

        success, connector_id, error_type = await deployer._verify_reply("部署完成 HTTP 200")

        self.assertFalse(success)
        self.assertIsNone(connector_id)
        self.assertEqual(error_type, VERIFY_FAILED)

    async def test_send_inject_classifies_timeout_sentinel_as_send_timeout(self):
        deployer = ClawDeployer.__new__(ClawDeployer)
        deployer.client = SimpleNamespace(send_message=lambda *a, **kw: asyncio.sleep(0, result="(等待最终态回复超时)"))
        deployer._send_timeout = 1

        with self.assertRaises(DeployError) as cm:
            await deployer._send_inject("prompt")

        self.assertEqual(cm.exception.error_type, SEND_TIMEOUT)

    async def test_send_inject_classifies_unavailable_sentinel_as_ws_disconnected(self):
        deployer = ClawDeployer.__new__(ClawDeployer)
        deployer.client = SimpleNamespace(send_message=lambda *a, **kw: asyncio.sleep(0, result="(发送失败，Websocket 未连接)"))
        deployer._send_timeout = 1

        with self.assertRaises(DeployError) as cm:
            await deployer._send_inject("prompt")

        self.assertEqual(cm.exception.error_type, WS_DISCONNECTED)

    async def test_deploy_reconnects_before_retrying_ws_disconnected_send(self):
        deployer = ClawDeployer.__new__(ClawDeployer)
        deployer._uid = "user-1"
        deployer._log_path = Path("conversation_log_user_user-1.jsonl")
        deployer._attempts = {}
        deployer._logger = logging.getLogger("test-claw-deployer")
        deployer._prompt_store = _PromptStore()
        deployer.client = _Client()
        deployer._max_attempts = 2
        deployer._base_backoff = 0
        deployer._tunnel_health = None

        send_calls = 0
        handshake_calls = 0

        async def ensure_instance():
            return False

        async def ws_handshake(wait_available):
            nonlocal handshake_calls
            handshake_calls += 1
            return True

        async def send_inject(prompt_text):
            nonlocal send_calls
            send_calls += 1
            if send_calls == 1:
                raise DeployError(WS_DISCONNECTED, "Websocket 未连接")
            return "部署完成 HTTP 200 Connector ID: 9c6e20f8-1111-2222-3333-444444444444"

        async def probe_status():
            return "AVAILABLE", 123, 200

        deployer._ensure_instance = ensure_instance
        deployer._ws_handshake = ws_handshake
        deployer._send_inject = send_inject
        deployer._probe_status = probe_status

        result = await deployer.deploy("deploy.v1.standard")

        self.assertTrue(result.success)
        self.assertEqual(send_calls, 2)
        self.assertEqual(handshake_calls, 2)

    async def test_deploy_records_final_instance_status_from_probe_status(self):
        deployer = ClawDeployer.__new__(ClawDeployer)
        deployer._uid = "user-1"
        deployer._log_path = Path("conversation_log_user_user-1.jsonl")
        deployer._attempts = {}
        deployer._logger = logging.getLogger("test-claw-deployer")
        deployer._prompt_store = _PromptStore()
        deployer.client = _Client()

        async def retry(phase, coro_factory, *, retryable_override=None):
            if phase == "ensure":
                return True, False, None
            if phase == "ws":
                return True, True, None
            if phase == "send":
                return True, "部署完成 HTTP 200", None
            raise AssertionError(f"unexpected phase: {phase}")

        async def verify_reply(reply):
            return True, "9c6e20f8-1111-2222-3333-444444444444", None

        async def probe_status():
            return "AVAILABLE", 123, 200

        deployer._retry = retry
        deployer._verify_reply = verify_reply
        deployer._probe_status = probe_status

        result = await deployer.deploy("deploy.v1.standard")

        self.assertTrue(result.success)
        self.assertEqual(result.instance_status, "AVAILABLE")
        self.assertEqual(result.instance_remain_sec, 123)


class ClawClientLoggingTests(unittest.TestCase):
    def test_safe_claw_trace_text_redacts_proxy_and_tunnel_secrets(self):
        rendered = safe_claw_trace_text(
            "TUNNEL_TOKEN=eyJsecret-token PROXY_API_KEY=proxy-secret "
            "CF_API_TOKEN=cf-secret CF_ACCOUNT_ID=account-secret PUBLIC_HOSTNAME=example.com",
            limit=500,
        )

        self.assertIn("TUNNEL_TOKEN=<redacted>", rendered)
        self.assertIn("PROXY_API_KEY=<redacted>", rendered)
        self.assertIn("CF_API_TOKEN=<redacted>", rendered)
        self.assertNotIn("eyJsecret-token", rendered)
        self.assertNotIn("proxy-secret", rendered)
        self.assertNotIn("cf-secret", rendered)

    def test_recording_client_redacts_secrets_in_recorded_payloads(self):
        client = RecordingClawClient.__new__(RecordingClawClient)
        client._record_file = io.StringIO()
        client._msg_seq = 0

        client._record(
            "out",
            {
                "params": {
                    "message": "TUNNEL_TOKEN=eyJsecret-token PROXY_API_KEY=proxy-secret"
                }
            },
        )

        recorded = json.loads(client._record_file.getvalue())
        message = recorded["data"]["params"]["message"]
        self.assertIn("TUNNEL_TOKEN=<redacted>", message)
        self.assertIn("PROXY_API_KEY=<redacted>", message)
        self.assertNotIn("eyJsecret-token", message)
        self.assertNotIn("proxy-secret", message)

    def test_recording_client_redacts_values_under_secret_keys(self):
        client = RecordingClawClient.__new__(RecordingClawClient)
        client._record_file = io.StringIO()
        client._msg_seq = 0

        client._record(
            "out",
            {
                "TUNNEL_TOKEN": "eyJsecret-token",
                "headers": {
                    "authorization": "Bearer proxy-secret",
                    "cookie": "serviceToken=service-secret",
                },
            },
        )

        recorded = json.loads(client._record_file.getvalue())
        self.assertEqual(recorded["data"]["TUNNEL_TOKEN"], "<redacted>")
        self.assertEqual(recorded["data"]["headers"]["authorization"], "<redacted>")
        self.assertEqual(recorded["data"]["headers"]["cookie"], "<redacted>")
        serialized = json.dumps(recorded, ensure_ascii=False)
        self.assertNotIn("eyJsecret-token", serialized)
        self.assertNotIn("proxy-secret", serialized)
        self.assertNotIn("service-secret", serialized)


if __name__ == "__main__":
    unittest.main()
