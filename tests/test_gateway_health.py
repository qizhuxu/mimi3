import unittest


class GatewayHealthTests(unittest.TestCase):
    def test_stats_url_is_derived_from_public_ws_url(self):
        from mimo2api.gateway_health import stats_url_from_ws_url

        self.assertEqual(
            stats_url_from_ws_url("wss://gateway.example.com/ws?node=abc"),
            "https://gateway.example.com/api/stats",
        )
        self.assertEqual(
            stats_url_from_ws_url("ws://1.2.3.4:8000/ws"),
            "http://1.2.3.4:8000/api/stats",
        )

    def test_parse_remote_stats_nodes_uses_node_uid(self):
        from mimo2api.gateway_health import parse_stats_nodes, summarize_stats_payload

        payload = {
            "active_clients": 3,
            "nodes": [
                {"node": "uid-1", "available": True, "last_seen_at": 1000.0},
                {"node": "Unknown", "available": True},
                {"node": "uid-2", "available": False, "connected_at": 900.0},
            ],
        }
        nodes = parse_stats_nodes(payload, source_url="https://gateway.example.com/api/stats")
        summary = summarize_stats_payload(payload)

        self.assertEqual(sorted(nodes), ["uid-1", "uid-2"])
        self.assertEqual(nodes["uid-1"].source, "remote")
        self.assertEqual(nodes["uid-1"].last_seen_at, 1000.0)
        self.assertEqual(nodes["uid-2"].connected_at, 900.0)
        self.assertEqual(summary["active_clients"], 3)
        self.assertEqual(summary["identified_nodes"], 2)
        self.assertEqual(summary["unknown_nodes"], 1)

    def test_parse_remote_stats_nodes_accepts_handshake_identity_fields(self):
        from mimo2api.gateway_health import parse_stats_nodes

        nodes = parse_stats_nodes(
            {"nodes": [{"node_id": "uid-from-hello"}, {"uid": "uid-from-stats"}]},
            source_url="https://gateway.example.com/api/stats",
        )

        self.assertEqual(sorted(nodes), ["uid-from-hello", "uid-from-stats"])
