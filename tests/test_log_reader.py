import shutil
import tempfile
import unittest
from pathlib import Path


class LogReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.logs_dir = self.tmp / "logs"
        self.logs_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_read_log_file_filters_limits_and_redacts_sensitive_values(self):
        from mimo2api.log_reader import read_log_file

        (self.logs_dir / "gateway.log").write_text(
            "\n".join(
                [
                    "2026-06-11 - [root] - INFO - boot serviceToken=info-secret",
                    "2026-06-11 - [root] - ERROR - needle Authorization: Bearer bearer-secret serviceToken=st-secret xiaomichatbot_ph=ph-secret",
                    "2026-06-11 - [root] - ERROR - needle Cookie: serviceToken=cookie-secret; token=token-secret",
                    "2026-06-11 - [root] - ERROR - needle session_secret=session-secret password=plain-secret",
                ]
            ),
            encoding="utf-8",
        )

        payload = read_log_file(
            logs_dir=self.logs_dir,
            filename="gateway.log",
            limit=10,
            level="ERROR",
            keyword="needle",
        )

        self.assertEqual(payload["file"], "gateway.log")
        self.assertEqual(payload["count"], 3)
        rendered = "\n".join(payload["lines"])
        self.assertIn("[REDACTED]", rendered)
        self.assertIn("needle", rendered)
        self.assertNotIn("bearer-secret", rendered)
        self.assertNotIn("st-secret", rendered)
        self.assertNotIn("ph-secret", rendered)
        self.assertNotIn("cookie-secret", rendered)
        self.assertNotIn("token-secret", rendered)
        self.assertNotIn("session-secret", rendered)
        self.assertNotIn("plain-secret", rendered)

    def test_read_log_file_rejects_path_traversal(self):
        from mimo2api.log_reader import read_log_file

        (self.tmp / "outside.log").write_text("outside", encoding="utf-8")

        with self.assertRaises(ValueError):
            read_log_file(logs_dir=self.logs_dir, filename="../outside.log")

    def test_list_log_files_only_returns_allowed_root_files(self):
        from mimo2api.log_reader import list_log_files

        (self.logs_dir / "gateway.log").write_text("ok", encoding="utf-8")
        (self.logs_dir / "gateway.log.1").write_text("rotated", encoding="utf-8")
        (self.logs_dir / "notes.txt").write_text("ok", encoding="utf-8")
        (self.logs_dir / "secret.env").write_text("SECRET=bad", encoding="utf-8")
        nested = self.logs_dir / "nested"
        nested.mkdir()
        (nested / "nested.log").write_text("nested", encoding="utf-8")

        payload = list_log_files(logs_dir=self.logs_dir)
        names = {item["name"] for item in payload["files"]}

        self.assertIn("gateway.log", names)
        self.assertIn("gateway.log.1", names)
        self.assertIn("notes.txt", names)
        self.assertNotIn("secret.env", names)
        self.assertNotIn("nested.log", names)
