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
