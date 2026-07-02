import unittest
import time
from pathlib import Path


class WebSocketHealthTests(unittest.TestCase):
    def setUp(self):
        from mimo2api.gateway_state import state

        state.active_clients.clear()
        state.node_to_ws.clear()
        state.ws_id_to_node.clear()
        state.node_connected_at.clear()
        state.node_last_seen_at.clear()

    def test_manager_online_check_uses_node_identity_mapping(self):
        from mimo2api.gateway_state import state
        from mimo2api.manager import AccountManager

        manager = AccountManager("target_uid", {"userId": "target_uid", "name": "test"})
        state.node_to_ws["other_uid"] = object()

        self.assertFalse(manager._node_online("target_uid"))

        state.node_to_ws["target_uid"] = object()
        self.assertTrue(manager._node_online("target_uid"))

    def test_lifecycle_marks_registered_but_silent_node_as_stale(self):
        from mimo2api.lifecycle_monitor import classify_lifecycle

        status = classify_lifecycle(
            cloud_status="AVAILABLE",
            remain_sec=1200,
            bridge_status="online",
            cooldown_remaining_seconds=0,
            stale=True,
            has_credentials=True,
        )

        self.assertEqual(status, "bridge_stale")

    def test_bridge_sends_periodic_heartbeat_messages(self):
        source = Path("mimo2api/bridge.py").read_text(encoding="utf-8")

        self.assertIn("BRIDGE_HEARTBEAT_SECONDS", source)
        self.assertIn("NODE_ID", source)
        self.assertIn('"type": "hello"', source)
        self.assertIn('"type": "heartbeat"', source)
        self.assertIn('"node": NODE_ID', source)

    def test_gateway_can_bind_node_from_hello_payload(self):
        import asyncio
        from mimo2api.gateway_state import state
        from mimo2api.web_service import bind_ws_node_from_payload

        class DummyWS:
            async def close(self, code=1000, reason=""):
                self.close_code = code
                self.close_reason = reason

        ws = DummyWS()
        state.active_clients.append(ws)

        node_id = asyncio.run(bind_ws_node_from_payload(ws, {"type": "hello", "node": "uid-from-hello"}, now=100.0))

        self.assertEqual(node_id, "uid-from-hello")
        self.assertIs(state.node_to_ws["uid-from-hello"], ws)
        self.assertEqual(state.ws_id_to_node[id(ws)], "uid-from-hello")
        self.assertEqual(state.node_last_seen_at["uid-from-hello"], 100.0)

    def test_gateway_duplicate_uid_replaces_old_ws(self):
        import asyncio
        from mimo2api.gateway_state import state
        from mimo2api.web_service import bind_ws_node

        class DummyWS:
            def __init__(self):
                self.close_code = None

            async def close(self, code=1000, reason=""):
                self.close_code = code
                self.close_reason = reason

        old_ws = DummyWS()
        new_ws = DummyWS()
        state.active_clients.extend([old_ws, new_ws])
        asyncio.run(bind_ws_node(old_ws, "uid-1", now=100.0))
        asyncio.run(bind_ws_node(new_ws, "uid-1", now=200.0))

        self.assertIs(state.node_to_ws["uid-1"], new_ws)
        self.assertEqual(old_ws.close_code, 4000)
        self.assertNotIn(old_ws, state.active_clients)

    def test_lifecycle_can_use_remote_gateway_nodes(self):
        from mimo2api.gateway_health import NodePresence
        from mimo2api.lifecycle_monitor import resolve_bridge_presence

        presence = resolve_bridge_presence(
            "uid-remote",
            remote_nodes={
                "uid-remote": NodePresence(
                    uid="uid-remote",
                    source="remote",
                    connected_at=1000.0,
                    last_seen_at=1010.0,
                    source_url="https://gateway.example.com/api/stats",
                )
            },
            now=1020.0,
            node_stale_seconds=90,
        )

        self.assertEqual(presence["bridge_status"], "online")
        self.assertEqual(presence["bridge_source"], "remote")
        self.assertEqual(presence["node_last_seen_at"], 1010.0)

    def test_lifecycle_marks_remote_unknown_nodes_as_ambiguous(self):
        from mimo2api.lifecycle_monitor import classify_lifecycle, resolve_bridge_presence

        presence = resolve_bridge_presence(
            "uid-missing",
            remote_nodes={},
            remote_meta={"unknown_nodes": "13"},
            now=1020.0,
            node_stale_seconds=90,
        )

        self.assertEqual(presence["bridge_status"], "ambiguous")
        self.assertEqual(presence["bridge_source"], "remote_unknown")
        self.assertEqual(
            classify_lifecycle(
                cloud_status="AVAILABLE",
                remain_sec=1200,
                bridge_status=presence["bridge_status"],
                cooldown_remaining_seconds=0,
                stale=False,
                has_credentials=True,
            ),
            "bridge_ambiguous",
        )

    def test_manager_wait_for_node_can_use_remote_gateway_stats(self):
        import mimo2api.manager as manager_module
        from mimo2api.gateway_health import NodePresence
        import logging

        async def fake_fetch_remote_gateway_nodes():
            return {
                "uid-remote": NodePresence(
                    uid="uid-remote",
                    source="remote",
                    connected_at=time.time(),
                    last_seen_at=time.time(),
                    source_url="https://gateway.example.com/api/stats",
                )
            }, {"url": "https://gateway.example.com/api/stats", "error": ""}

        original = manager_module.fetch_remote_gateway_nodes
        manager_module.fetch_remote_gateway_nodes = fake_fetch_remote_gateway_nodes
        logging.getLogger("Acc-test-uid-remote").disabled = True
        try:
            manager = manager_module.AccountManager("uid-remote", {"userId": "uid-remote", "name": "test"})
            self.assertTrue(__import__("asyncio").run(manager._wait_for_node("uid-remote", timeout=1)))
        finally:
            logging.getLogger("Acc-test-uid-remote").disabled = False
            manager_module.fetch_remote_gateway_nodes = original

    def test_manager_wait_for_node_stops_on_remote_unknown_growth(self):
        import asyncio
        import logging
        import mimo2api.manager as manager_module

        snapshots = [
            ({}, {"url": "https://gateway.example.com/api/stats", "error": "", "active_clients": "0", "unknown_nodes": "0"}),
            ({}, {"url": "https://gateway.example.com/api/stats", "error": "", "active_clients": "1", "unknown_nodes": "1"}),
        ]

        async def fake_fetch_remote_gateway_nodes():
            return snapshots.pop(0) if snapshots else ({}, {"url": "https://gateway.example.com/api/stats", "error": "", "active_clients": "1", "unknown_nodes": "1"})

        original = manager_module.fetch_remote_gateway_nodes
        manager_module.fetch_remote_gateway_nodes = fake_fetch_remote_gateway_nodes
        logging.getLogger("Acc-test-uid-unknown").disabled = True
        try:
            manager = manager_module.AccountManager("uid-unknown", {"userId": "uid-unknown", "name": "test"})
            self.assertTrue(asyncio.run(manager._wait_for_node("uid-unknown", timeout=1)))
        finally:
            logging.getLogger("Acc-test-uid-unknown").disabled = False
            manager_module.fetch_remote_gateway_nodes = original

    def test_manager_node_online_anywhere_can_use_remote_gateway_stats(self):
        import asyncio
        import logging
        import mimo2api.manager as manager_module
        from mimo2api.gateway_health import NodePresence

        async def fake_fetch_remote_gateway_nodes():
            return {
                "uid-remote": NodePresence(
                    uid="uid-remote",
                    source="remote",
                    source_url="https://gateway.example.com/api/stats",
                )
            }, {"url": "https://gateway.example.com/api/stats", "error": ""}

        original = manager_module.fetch_remote_gateway_nodes
        manager_module.fetch_remote_gateway_nodes = fake_fetch_remote_gateway_nodes
        logging.getLogger("Acc-test-uid-remote").disabled = True
        try:
            manager = manager_module.AccountManager("uid-remote", {"userId": "uid-remote", "name": "test"})
            self.assertTrue(asyncio.run(manager._node_online_anywhere("uid-remote")))
        finally:
            logging.getLogger("Acc-test-uid-remote").disabled = False
            manager_module.fetch_remote_gateway_nodes = original

    def test_manager_skips_injection_when_remote_gateway_has_only_unknown_nodes(self):
        import asyncio
        import logging
        import mimo2api.manager as manager_module

        class DummyClient:
            def __init__(self):
                self.sent = 0

            async def send_message(self, text, timeout=120):
                self.sent += 1
                return "sent"

        async def fake_fetch_remote_gateway_nodes():
            return {}, {
                "url": "https://gateway.example.com/api/stats",
                "error": "",
                "active_clients": "13",
                "identified_nodes": "0",
                "unknown_nodes": "13",
            }

        original = manager_module.fetch_remote_gateway_nodes
        manager_module.fetch_remote_gateway_nodes = fake_fetch_remote_gateway_nodes
        logging.getLogger("Acc-test-uid-unknown").disabled = True
        try:
            client = DummyClient()
            manager = manager_module.AccountManager("uid-unknown", {"userId": "uid-unknown", "name": "test"})
            self.assertTrue(asyncio.run(manager.inject_bridge_with_retry(client, "prompt", max_retries=3, ws_wait_timeout=1)))
            self.assertEqual(client.sent, 0)
        finally:
            logging.getLogger("Acc-test-uid-unknown").disabled = False
            manager_module.fetch_remote_gateway_nodes = original

    def test_wait_for_node_uses_wall_clock_deadline_when_remote_stats_is_slow(self):
        import asyncio
        import logging
        import mimo2api.manager as manager_module

        now = [0.0]
        fetch_calls = 0
        sleep_calls = 0

        async def fake_fetch_remote_gateway_nodes():
            nonlocal fetch_calls
            fetch_calls += 1
            now[0] += 5.0
            return {}, {"url": "https://gateway.example.com/api/stats", "error": "slow"}

        async def fake_sleep(seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            now[0] += float(seconds)

        original_fetch = manager_module.fetch_remote_gateway_nodes
        original_monotonic = manager_module.time.monotonic
        original_sleep = manager_module.asyncio.sleep
        manager_module.fetch_remote_gateway_nodes = fake_fetch_remote_gateway_nodes
        manager_module.time.monotonic = lambda: now[0]
        manager_module.asyncio.sleep = fake_sleep
        logging.getLogger("Acc-test-uid-deadline").disabled = True
        try:
            manager = manager_module.AccountManager("uid-deadline", {"userId": "uid-deadline", "name": "test"})
            result = asyncio.run(manager._wait_for_node_status("uid-deadline", timeout=2))
        finally:
            logging.getLogger("Acc-test-uid-deadline").disabled = False
            manager_module.fetch_remote_gateway_nodes = original_fetch
            manager_module.time.monotonic = original_monotonic
            manager_module.asyncio.sleep = original_sleep

        self.assertFalse(result.ok)
        self.assertEqual(fetch_calls, 1)
        self.assertEqual(sleep_calls, 0)

    def test_claw_chat_trace_logs_are_redacted_and_truncated(self):
        import asyncio
        import logging
        from mimo2api.manager import NativeClawClient

        logger = logging.getLogger("test-claw-chat-trace")
        logger.setLevel(logging.INFO)
        client = NativeClawClient(
            "ph-secret-value",
            {
                "userId": "uid-trace",
                "serviceToken": "service-secret-value",
                "xiaomichatbot_ph": "ph-secret-value",
            },
            logger,
        )
        client.connected = True

        class DummyWs:
            async def send(self, payload):
                client.events.append({
                    "event": "chat",
                    "payload": {
                        "state": "final",
                        "message": {
                            "role": "assistant",
                            "content": [{
                                "type": "text",
                                "text": "reply " + ("r" * 500) + " service-secret-value ph-secret-value",
                            }],
                        },
                    },
                })

        client.ws = DummyWs()
        prompt = (
            "run bridge "
            "serviceToken=service-secret-value "
            "xiaomichatbot_ph=ph-secret-value "
            "Cookie: serviceToken=service-secret-value; xiaomichatbot_ph=ph-secret-value "
            + ("p" * 600)
        )

        with self.assertLogs(logger, level="INFO") as captured:
            reply = asyncio.run(client.send_message(prompt, timeout=1, stage="bridge.inject"))

        raw_logs = "\n".join(captured.output)
        self.assertIn("claw.chat.send", raw_logs)
        self.assertIn("claw.chat.reply", raw_logs)
        self.assertIn("phase=bridge.inject", raw_logs)
        self.assertIn("request_id=", raw_logs)
        self.assertIn("sha1=", raw_logs)
        self.assertNotIn("service-secret-value", raw_logs)
        self.assertNotIn("ph-secret-value", raw_logs)
        self.assertIn("reply", reply)

    def test_claw_chat_trace_logs_prompt_id_and_redacts_api_key_values(self):
        import asyncio
        import logging
        from mimo2api.manager import NativeClawClient

        logger = logging.getLogger("test-claw-chat-prompt-id")
        logger.setLevel(logging.INFO)
        client = NativeClawClient("ph", {"userId": "uid-prompt-id"}, logger)
        client.connected = True

        class DummyWs:
            async def send(self, payload):
                client.events.append({
                    "event": "chat",
                    "payload": {
                        "state": "final",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "ok"}],
                        },
                    },
                })

        client.ws = DummyWs()
        prompt = "MIMO_API_KEY=sk-secret-value MIMO_API_ENDPOINT=https://secret.example/v1/chat/completions"

        with self.assertLogs(logger, level="INFO") as captured:
            asyncio.run(client.send_message(prompt, timeout=1, stage="bridge.inject", prompt_id="bridge.v1.standard"))

        raw_logs = "\n".join(captured.output)
        self.assertIn("prompt_id=bridge.v1.standard", raw_logs)
        self.assertNotIn("sk-secret-value", raw_logs)

    def test_bridge_injection_failure_classification_recognizes_retry_causes(self):
        from mimo2api.manager import bridge_injection_failure_classes, classify_bridge_injection_failure

        cases = [
            ("我的安全策略禁止执行 pip install 等安装命令", "dependency_install_refused"),
            ("ModuleNotFoundError: No module named 'websockets'", "dependency_missing"),
            ("缺失模块：websockets 和 httpx。", "dependency_missing"),
            ("DEPENDENCY_MISSING modules=websockets,httpx", "dependency_missing"),
            ("它用本机 API Key 代理外部请求，本质是把凭据暴露给第三方，我不会执行", "proxy_or_api_key_refused"),
            ("请你手动运行命令，安装完成后告诉我", "manual_action_requested"),
            ("(等待最终态回复超时)", "timeout"),
            ("抱歉，我不能执行这个请求", "generic_refusal"),
        ]

        for reply, expected in cases:
            with self.subTest(expected=expected):
                self.assertIn(expected, bridge_injection_failure_classes(reply))
                self.assertEqual(classify_bridge_injection_failure(reply), expected)

    def test_bridge_prompt_selector_does_not_repeat_prompt_ids_across_retries(self):
        import asyncio
        import logging
        import mimo2api.manager as manager_module

        class DummyClient:
            def __init__(self):
                self.prompt_ids = []

            async def send_message(self, text, timeout=120, stage="chat", prompt_id=None):
                if stage == "bridge.inject":
                    self.prompt_ids.append(prompt_id)
                    if len(self.prompt_ids) == 1:
                        return "我的安全策略禁止执行 pip install 等安装命令"
                    if len(self.prompt_ids) == 2:
                        return "它用本机 API Key 代理外部请求，本质是把凭据暴露给第三方，我不会执行"
                    return "我已完成"
                return "reset ok"

        async def fake_fetch_remote_gateway_nodes():
            return {}, {"url": "", "error": "local stats skipped"}

        async def fake_wait_for_node_status(node_id, timeout=90, baseline_remote_meta=None):
            return manager_module.NodeWaitResult(reason="timeout")

        original_fetch = manager_module.fetch_remote_gateway_nodes
        original_sleep = manager_module.asyncio.sleep
        manager_module.fetch_remote_gateway_nodes = fake_fetch_remote_gateway_nodes
        async def fake_sleep(seconds):
            await original_sleep(0)

        manager_module.asyncio.sleep = fake_sleep
        logging.getLogger("Acc-test-uid-prompt-rotate").disabled = True
        try:
            client = DummyClient()
            manager = manager_module.AccountManager("uid-prompt-rotate", {"userId": "uid-prompt-rotate", "name": "test"})
            manager._wait_for_node_status = fake_wait_for_node_status
            prompts = manager_module.build_bridge_injection_prompt_library("BRIDGE_CODE")
            result = asyncio.run(manager.inject_bridge_with_retry(
                client,
                prompts,
                max_retries=3,
                ws_wait_timeout=90,
                label="桥接脚本(测试)",
            ))
        finally:
            logging.getLogger("Acc-test-uid-prompt-rotate").disabled = False
            manager_module.fetch_remote_gateway_nodes = original_fetch
            manager_module.asyncio.sleep = original_sleep

        self.assertFalse(result)
        self.assertEqual(len(client.prompt_ids), 3)
        self.assertEqual(len(set(client.prompt_ids)), 3)

    def test_bridge_injection_uses_short_node_wait_after_explicit_refusal(self):
        import asyncio
        import logging
        import mimo2api.manager as manager_module

        class DummyClient:
            async def send_message(self, text, timeout=120, stage="chat", prompt_id=None):
                if stage == "bridge.inject":
                    return "我的安全策略禁止执行 pip install 等安装命令"
                return "reset ok"

        async def fake_fetch_remote_gateway_nodes():
            return {}, {"url": "", "error": "local stats skipped"}

        wait_timeouts = []

        async def fake_wait_for_node_status(node_id, timeout=90, baseline_remote_meta=None):
            wait_timeouts.append(timeout)
            return manager_module.NodeWaitResult(reason="timeout")

        original_fetch = manager_module.fetch_remote_gateway_nodes
        original_sleep = manager_module.asyncio.sleep
        manager_module.fetch_remote_gateway_nodes = fake_fetch_remote_gateway_nodes
        async def fake_sleep(seconds):
            await original_sleep(0)

        manager_module.asyncio.sleep = fake_sleep
        logging.getLogger("Acc-test-uid-short-wait").disabled = True
        try:
            manager = manager_module.AccountManager("uid-short-wait", {"userId": "uid-short-wait", "name": "test"})
            manager._wait_for_node_status = fake_wait_for_node_status
            prompts = manager_module.build_bridge_injection_prompt_library("BRIDGE_CODE")
            asyncio.run(manager.inject_bridge_with_retry(
                DummyClient(),
                prompts,
                max_retries=2,
                ws_wait_timeout=90,
                label="桥接脚本(测试)",
            ))
        finally:
            logging.getLogger("Acc-test-uid-short-wait").disabled = False
            manager_module.fetch_remote_gateway_nodes = original_fetch
            manager_module.asyncio.sleep = original_sleep

        self.assertEqual(wait_timeouts[0], manager_module.BRIDGE_REFUSAL_NODE_WAIT_SECONDS)

    def test_bridge_injection_uses_short_node_wait_after_dependency_missing(self):
        from mimo2api.manager import BRIDGE_REFUSAL_NODE_WAIT_SECONDS, _bridge_node_wait_timeout

        self.assertEqual(
            _bridge_node_wait_timeout(("dependency_missing",), 90),
            BRIDGE_REFUSAL_NODE_WAIT_SECONDS,
        )

    def test_bridge_injection_logs_send_and_node_wait_stages(self):
        import asyncio
        import logging
        import mimo2api.manager as manager_module

        class DummyClient:
            async def send_message(self, text, timeout=120, stage="chat", prompt_id=None):
                return "assistant finished"

        async def fake_fetch_remote_gateway_nodes():
            return {}, {"url": "", "error": "local stats skipped"}

        async def fake_wait_for_node_status(node_id, timeout=90, baseline_remote_meta=None):
            return manager_module.NodeWaitResult(reason="timeout")

        original_fetch = manager_module.fetch_remote_gateway_nodes
        manager_module.fetch_remote_gateway_nodes = fake_fetch_remote_gateway_nodes
        logger = logging.getLogger("Acc-test-uid-inject-log")
        logger.setLevel(logging.INFO)
        try:
            manager = manager_module.AccountManager("uid-inject-log", {"userId": "uid-inject-log", "name": "test"})
            manager._wait_for_node_status = fake_wait_for_node_status
            with self.assertLogs(logger, level="INFO") as captured:
                result = asyncio.run(manager.inject_bridge_with_retry(
                    DummyClient(),
                    "prompt",
                    max_retries=1,
                    ws_wait_timeout=1,
                    label="桥接脚本(测试)",
                ))
        finally:
            manager_module.fetch_remote_gateway_nodes = original_fetch

        raw_logs = "\n".join(captured.output)
        self.assertFalse(result)
        self.assertIn("bridge.inject.chat_send.start", raw_logs)
        self.assertIn("bridge.inject.chat_send.done", raw_logs)
        self.assertIn("bridge.inject.node_wait.start", raw_logs)
        self.assertIn("bridge.inject.node_wait.done", raw_logs)


