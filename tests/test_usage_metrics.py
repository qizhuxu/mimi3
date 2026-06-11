import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path


class UsageMetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = self.tmp / "usage_metrics.db"
        import mimo2api.metrics_store as metrics_store
        from mimo2api.gateway_state import state

        self.metrics_store = metrics_store
        self.state = state
        self.original_db_path = metrics_store.METRICS_DB_PATH
        self.original_retention_days = metrics_store.METRICS_RETENTION_DAYS
        metrics_store.METRICS_DB_PATH = str(self.db_path)
        state.metrics = state._default_metrics()

    def tearDown(self):
        self.metrics_store.METRICS_DB_PATH = self.original_db_path
        self.metrics_store.METRICS_RETENTION_DAYS = self.original_retention_days
        self.state.metrics = self.state._default_metrics()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_request_finished_records_real_usage_by_hour_model_and_route(self):
        self.metrics_store.init_metrics_db()
        created_at = 1_785_000_123
        started_at = time.monotonic() - 0.01

        self.metrics_store.record_request_finished(
            route_key="/v1/chat/completions",
            status_code=200,
            started_at=started_at,
            first_byte_at=started_at + 0.001,
            success=True,
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            model="mimo-v2.5",
            created_at=created_at,
        )
        self.metrics_store.record_request_finished(
            route_key="/v1/chat/completions",
            status_code=200,
            started_at=started_at,
            first_byte_at=started_at + 0.001,
            success=True,
            usage={"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10},
            model="mimo-v2.5",
            created_at=created_at + 60,
        )

        stats = self.metrics_store.load_usage_stats(
            start_ts=created_at - 3600,
            end_ts=created_at + 3600,
        )

        self.assertEqual(stats["summary"]["requests_with_usage"], 2)
        self.assertEqual(stats["summary"]["requests_succeeded"], 2)
        self.assertEqual(stats["summary"]["prompt_tokens"], 13)
        self.assertEqual(stats["summary"]["completion_tokens"], 27)
        self.assertEqual(stats["summary"]["total_tokens"], 40)
        self.assertEqual(len(stats["buckets"]), 1)
        self.assertEqual(stats["buckets"][0]["model"], "mimo-v2.5")
        self.assertEqual(stats["buckets"][0]["route"], "/v1/chat/completions")

    def test_extract_usage_from_json_body_and_sse_chunk(self):
        json_usage = self.metrics_store.extract_usage_from_sse_chunk(
            '{"id":"abc","usage":{"prompt_tokens":2,"completion_tokens":3,"total_tokens":5}}'
        )
        sse_usage = self.metrics_store.extract_usage_from_sse_chunk(
            'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":11,"total_tokens":18}}\n\n'
        )

        self.assertEqual(json_usage["total_tokens"], 5)
        self.assertEqual(sse_usage["prompt_tokens"], 7)

    def test_missing_usage_does_not_create_usage_bucket(self):
        self.metrics_store.init_metrics_db()
        started_at = time.monotonic() - 0.01

        self.metrics_store.record_request_finished(
            route_key="/v1/chat/completions",
            status_code=200,
            started_at=started_at,
            first_byte_at=started_at + 0.001,
            success=True,
            usage=None,
            model="mimo-v2.5",
            created_at=1_785_000_123,
        )

        stats = self.metrics_store.load_usage_stats(start_ts=1_785_000_000, end_ts=1_785_004_000)

        self.assertEqual(stats["summary"]["requests_with_usage"], 0)
        self.assertEqual(stats["summary"]["total_tokens"], 0)
        self.assertEqual(stats["buckets"], [])

    def test_empty_usage_payload_does_not_create_usage_bucket(self):
        self.metrics_store.init_metrics_db()

        stored = self.metrics_store.record_usage_bucket(
            route_key="/v1/chat/completions",
            model="mimo-v2.5",
            usage={},
            success=True,
            created_at=1_785_000_123,
        )
        stats = self.metrics_store.load_usage_stats(start_ts=1_785_000_000, end_ts=1_785_004_000)

        self.assertFalse(stored)
        self.assertEqual(stats["summary"]["requests_with_usage"], 0)
        self.assertEqual(stats["buckets"], [])

    def test_usage_stats_filters_model_and_date_range(self):
        self.metrics_store.init_metrics_db()
        day_one = 1_785_024_000
        day_two = day_one + 86_400

        self.metrics_store.record_usage_bucket(
            route_key="/v1/chat/completions",
            model="mimo-a",
            usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            success=True,
            created_at=day_one + 120,
        )
        self.metrics_store.record_usage_bucket(
            route_key="/v1/chat/completions",
            model="mimo-b",
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            success=True,
            created_at=day_one + 180,
        )
        self.metrics_store.record_usage_bucket(
            route_key="/v1/responses",
            model="mimo-a",
            usage={"input_tokens": 5, "output_tokens": 6, "total_tokens": 11},
            success=False,
            created_at=day_two + 120,
        )

        stats = self.metrics_store.load_usage_stats(
            start_date="2026-07-26",
            end_date="2026-07-26",
            model="mimo-a",
        )

        self.assertEqual(stats["summary"]["requests_with_usage"], 1)
        self.assertEqual(stats["summary"]["requests_succeeded"], 1)
        self.assertEqual(stats["summary"]["requests_failed"], 0)
        self.assertEqual(stats["summary"]["total_tokens"], 3)
        self.assertEqual({item["model"] for item in stats["models"]}, {"mimo-a"})

    def test_usage_retention_cleanup_removes_old_buckets(self):
        self.metrics_store.METRICS_RETENTION_DAYS = 1
        self.metrics_store.init_metrics_db()
        now = 1_785_200_000
        old_ts = now - 3 * 86_400
        fresh_ts = now - 60

        self.metrics_store.record_usage_bucket(
            route_key="/v1/chat/completions",
            model="old",
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            success=True,
            created_at=old_ts,
            now=old_ts,
        )
        self.metrics_store.record_usage_bucket(
            route_key="/v1/chat/completions",
            model="fresh",
            usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            success=True,
            created_at=fresh_ts,
            now=now,
        )

        stats = self.metrics_store.load_usage_stats(start_ts=old_ts - 3600, end_ts=now + 3600)

        self.assertEqual({item["model"] for item in stats["models"]}, {"fresh"})
        self.assertEqual(stats["summary"]["total_tokens"], 5)


class UsageMetricsApiTests(unittest.TestCase):
    def test_usage_api_returns_usage_stats_payload(self):
        import asyncio
        import mimo2api.web_service as web_service

        original = web_service.load_usage_stats
        calls = []
        web_service.load_usage_stats = lambda **kwargs: calls.append(kwargs) or {
            "summary": {"total_tokens": 42},
            "buckets": [],
            "models": [],
        }
        try:
            response = asyncio.run(
                web_service.api_usage_stats(
                    hours=12,
                    start_date="2026-07-23",
                    end_date="2026-07-24",
                    model="mimo-v2.5",
                )
            )
        finally:
            web_service.load_usage_stats = original

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["summary"]["total_tokens"], 42)
        self.assertEqual(calls[0]["hours"], 12)
        self.assertEqual(calls[0]["model"], "mimo-v2.5")
