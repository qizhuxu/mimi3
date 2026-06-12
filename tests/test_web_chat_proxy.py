import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path


class WebChatProxyMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.users_dir = self.tmp / "users"
        self.users_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_user(self, uid: str, data: dict):
        path = self.users_dir / f"user_{uid}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_proxy_metadata_contains_safe_urls_and_no_secrets(self):
        from mimo2api.web_chat_proxy import build_web_chat_proxy_metadata

        metadata = build_web_chat_proxy_metadata(
            "123",
            {
                "userId": "123",
                "serviceToken": "service-secret",
                "xiaomichatbot_ph": "ph-secret",
            },
        )
        raw = json.dumps(metadata, ensure_ascii=False)

        self.assertEqual(metadata["uid"], "123")
        self.assertTrue(metadata["credentials"]["userId"])
        self.assertTrue(metadata["credentials"]["serviceToken"])
        self.assertTrue(metadata["credentials"]["xiaomichatbot_ph"])
        self.assertEqual(
            [item["path"] for item in metadata["endpoints"]],
            [
                "/api/web-chat/123/open-apis/bot/chat",
                "/api/web-chat/123/open-apis/chat/conversation/list",
                "/api/web-chat/123/open-apis/chat/dialog/list",
                "/api/web-chat/123/ws/proxy",
            ],
        )
        self.assertNotIn("service-secret", raw)
        self.assertNotIn("ph-secret", raw)

    def test_proxy_metadata_marks_missing_credentials_without_echoing_values(self):
        from mimo2api.web_chat_proxy import build_web_chat_proxy_metadata

        metadata = build_web_chat_proxy_metadata("456", {"userId": "456"})

        self.assertTrue(metadata["credentials"]["userId"])
        self.assertFalse(metadata["credentials"]["serviceToken"])
        self.assertFalse(metadata["credentials"]["xiaomichatbot_ph"])

    def test_user_loader_rejects_invalid_uid_and_mismatched_file(self):
        import mimo2api.web_chat_proxy as web_chat_proxy

        original_users_dir = web_chat_proxy.USERS_DIR
        web_chat_proxy.USERS_DIR = self.users_dir
        try:
            self.write_user(
                "123",
                {
                    "userId": "999",
                    "serviceToken": "service-secret",
                    "xiaomichatbot_ph": "ph-secret",
                },
            )

            self.assertIsNone(web_chat_proxy._load_user("12x"))
            self.assertIsNone(web_chat_proxy._load_user("123"))
        finally:
            web_chat_proxy.USERS_DIR = original_users_dir

    def test_path_whitelist_does_not_allow_arbitrary_proxy_targets(self):
        from mimo2api.web_chat_proxy import _sanitize_path

        self.assertEqual(
            _sanitize_path("chat/conversation/list"),
            "open-apis/chat/conversation/list",
        )
        self.assertIsNone(_sanitize_path("open-apis/user/ws/ticket"))
        self.assertIsNone(_sanitize_path("https://example.com/open-apis/chat"))
        self.assertIsNone(_sanitize_path("open-apis/chat/../user/ws/ticket"))


class WebChatProxyUsersApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.users_dir = self.tmp / "users"
        self.users_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_users_list_includes_safe_web_chat_metadata(self):
        import mimo2api.ui_router as ui_router

        user_data = {
            "userId": "686621",
            "name": "Account A",
            "serviceToken": "service-secret",
            "xiaomichatbot_ph": "ph-secret",
        }
        (self.users_dir / "user_686621.json").write_text(
            json.dumps(user_data),
            encoding="utf-8",
        )
        original_users_dir = ui_router.USERS_DIR
        original_fetch_user_status = ui_router.fetch_user_status

        async def fake_fetch_user_status(data):
            return {**data, "claw_status": "AVAILABLE", "remain_sec": 3600}

        ui_router.USERS_DIR = str(self.users_dir)
        ui_router.fetch_user_status = fake_fetch_user_status
        try:
            response = asyncio.run(ui_router.api_users_list())
        finally:
            ui_router.USERS_DIR = original_users_dir
            ui_router.fetch_user_status = original_fetch_user_status

        payload = json.loads(response.body)
        user = payload["users"][0]
        raw = json.dumps(payload, ensure_ascii=False)

        self.assertEqual(user["userId"], "686621")
        self.assertTrue(user["webChatProxy"]["credentials"]["serviceToken"])
        self.assertTrue(user["webChatProxy"]["credentials"]["xiaomichatbot_ph"])
        self.assertEqual(
            user["webChatProxy"]["endpoints"][0]["path"],
            "/api/web-chat/686621/open-apis/bot/chat",
        )
        self.assertNotIn("service-secret", raw)
        self.assertNotIn("ph-secret", raw)


class WebChatProxyWebUiTests(unittest.TestCase):
    def test_webui_exposes_proxy_panel_and_copy_action(self):
        html = Path("mimo2api/webui.html").read_text(encoding="utf-8")

        self.assertIn('id="webChatProxyPanel"', html)
        self.assertIn("selectedWebChatUid", html)
        self.assertIn("function renderWebChatProxyPanel", html)
        self.assertIn("function copyWebChatEndpoint", html)
