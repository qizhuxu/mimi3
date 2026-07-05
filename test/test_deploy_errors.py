import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from deploy_errors import (
    ALL,
    AUTH_EXPIRED,
    CREATE_FAILED,
    CREATE_PEAK_RATE_LIMITED,
    CREATE_RATE_LIMITED,
    DEPLOY_REFUSED,
    HANDSHAKE_TIMEOUT,
    NETWORK_ERROR,
    PROTOCOL_MISMATCH,
    SEND_TIMEOUT,
    SUCCESS,
    TICKET_SYNC_DELAY,
    VERIFY_FAILED,
    WS_CONNECT_FAILED,
    WS_DISCONNECTED,
    classify_http_error,
    classify_instance_status,
    classify_reply,
    classify_ws_error,
    extract_connector_id,
    is_retryable,
    is_strategy_swap,
    is_terminal,
    needs_relogin,
)


class ConnectionClosedForTest(Exception):
    pass


class DeployErrorClassificationTests(unittest.TestCase):
    def test_all_error_types_are_reachable_from_classifiers(self):
        observed = {
            classify_http_error(401),
            classify_http_error(200, code=7001),
            classify_http_error(429),
            classify_instance_status("CREATE_FAILED"),
            classify_ws_error(Exception("PROTOCOL_MISMATCH")),
            classify_ws_error(ConnectionRefusedError("refused")),
            classify_ws_error(TimeoutError("timeout"), phase="handshake"),
            classify_http_error(400),
            classify_ws_error(TimeoutError("timeout"), phase="send"),
            classify_ws_error(ConnectionClosedForTest("closed")),
            classify_http_error(500),
            classify_reply("DEPENDENCY_MISSING pip install"),
            classify_reply(""),
            classify_reply("部署完成 HTTP 200"),
        }

        self.assertEqual(observed, ALL)

    def test_http_error_priority_and_non_errors(self):
        self.assertEqual(classify_http_error(401, code=7001), AUTH_EXPIRED)
        self.assertEqual(classify_http_error(500, code=7001), CREATE_RATE_LIMITED)
        self.assertEqual(classify_http_error(429), CREATE_PEAK_RATE_LIMITED)
        self.assertEqual(classify_http_error(400), TICKET_SYNC_DELAY)
        self.assertEqual(classify_http_error(500), NETWORK_ERROR)
        self.assertIsNone(classify_http_error(403))
        self.assertIsNone(classify_http_error(200))

    def test_instance_status_mapping(self):
        self.assertEqual(classify_instance_status("AVAILABLE"), SUCCESS)
        self.assertEqual(classify_instance_status("CREATE_FAILED"), CREATE_FAILED)
        self.assertEqual(classify_instance_status("ERROR"), CREATE_FAILED)
        self.assertIsNone(classify_instance_status("DESTROYED"))
        self.assertIsNone(classify_instance_status("CREATING"))
        self.assertIsNone(classify_instance_status(""))

    def test_ws_error_mapping(self):
        self.assertEqual(classify_ws_error(Exception("PROTOCOL_MISMATCH")), PROTOCOL_MISMATCH)
        self.assertEqual(classify_ws_error(Exception("Unauthorized 401")), AUTH_EXPIRED)
        self.assertEqual(classify_ws_error(ConnectionClosedForTest("closed")), WS_DISCONNECTED)
        self.assertEqual(classify_ws_error(ConnectionRefusedError("refused")), WS_CONNECT_FAILED)
        self.assertEqual(classify_ws_error(TimeoutError("timeout"), phase="handshake"), HANDSHAKE_TIMEOUT)
        self.assertEqual(classify_ws_error(TimeoutError("timeout"), phase="send"), SEND_TIMEOUT)
        self.assertEqual(classify_ws_error(RuntimeError("boom")), NETWORK_ERROR)

    def test_reply_mapping_and_connector_extraction(self):
        self.assertEqual(classify_reply("全部通过，部署完成。Invalid tunnel secret | 无"), SUCCESS)
        self.assertEqual(classify_reply("无法安装 cloudflared"), DEPLOY_REFUSED)
        self.assertEqual(classify_reply("guardrail refused"), DEPLOY_REFUSED)
        self.assertEqual(classify_reply(""), VERIFY_FAILED)
        self.assertEqual(classify_reply("没有明确成功标记"), VERIFY_FAILED)
        self.assertEqual(
            extract_connector_id("Generated Connector ID: d8733b5a-7c1c-4a36-9dc7-2e43fbb23693"),
            "d8733b5a-7c1c-4a36-9dc7-2e43fbb23693",
        )

    def test_error_strategy_sets_only_reference_known_error_types(self):
        self.assertTrue(needs_relogin(AUTH_EXPIRED))
        self.assertTrue(is_terminal(AUTH_EXPIRED))
        self.assertTrue(is_terminal(CREATE_FAILED))
        self.assertTrue(is_terminal(PROTOCOL_MISMATCH))
        self.assertTrue(is_terminal(SUCCESS))
        self.assertTrue(is_retryable(WS_CONNECT_FAILED))
        self.assertTrue(is_retryable(HANDSHAKE_TIMEOUT))
        self.assertTrue(is_retryable(TICKET_SYNC_DELAY))
        self.assertTrue(is_retryable(SEND_TIMEOUT))
        self.assertTrue(is_retryable(WS_DISCONNECTED))
        self.assertTrue(is_retryable(NETWORK_ERROR))
        self.assertTrue(is_strategy_swap(DEPLOY_REFUSED))
        self.assertTrue(is_strategy_swap(VERIFY_FAILED))
        self.assertTrue(is_strategy_swap(CREATE_RATE_LIMITED))
        self.assertFalse(is_terminal(CREATE_PEAK_RATE_LIMITED))
        self.assertFalse(is_retryable(CREATE_PEAK_RATE_LIMITED))
        self.assertFalse(is_strategy_swap(CREATE_PEAK_RATE_LIMITED))


if __name__ == "__main__":
    unittest.main()
