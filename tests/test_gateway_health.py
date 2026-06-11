import unittest
import asyncio


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

    def test_remote_gateway_fetch_uses_configured_stats_proxy(self):
        import mimo2api.gateway_health as gateway_health

        created_kwargs = []

        class DummyResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"active_clients": 0, "nodes": []}

        class DummyAsyncClient:
            def __init__(self, **kwargs):
                created_kwargs.append(kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def get(self, url):
                self.url = url
                return DummyResponse()

        original_client = gateway_health.httpx.AsyncClient
        original_effective_ws_url = gateway_health.effective_ws_url
        original_get_config_value = getattr(gateway_health, "get_config_value", None)
        proxy = "http://user:pass@127.0.0.1:8080"

        gateway_health.httpx.AsyncClient = DummyAsyncClient
        gateway_health.effective_ws_url = lambda: "wss://gateway.example.com/ws"
        gateway_health.get_config_value = lambda key, default=None: proxy if key == "gateway.stats_proxy" else default
        try:
            nodes, meta = asyncio.run(gateway_health.fetch_remote_gateway_nodes())
        finally:
            gateway_health.httpx.AsyncClient = original_client
            gateway_health.effective_ws_url = original_effective_ws_url
            if original_get_config_value is None:
                delattr(gateway_health, "get_config_value")
            else:
                gateway_health.get_config_value = original_get_config_value

        self.assertEqual(nodes, {})
        self.assertEqual(meta["url"], "https://gateway.example.com/api/stats")
        self.assertEqual(created_kwargs[0]["proxy"], proxy)

    def test_gateway_stats_proxy_is_runtime_config_field(self):
        from mimo2api.runtime_config import FIELDS

        field = FIELDS["gateway.stats_proxy"]

        self.assertEqual(field.env, "MIMO_GATEWAY_STATS_PROXY")
        self.assertEqual(field.group, "访问与网关")
