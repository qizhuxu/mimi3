import json
import shutil
import tempfile
import unittest
from pathlib import Path


class UserBatchImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.users_dir = self.tmp / "users"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read_user(self, uid):
        return json.loads((self.users_dir / f"user_{uid}.json").read_text("utf-8"))

    def test_batch_import_accepts_array_and_keeps_partial_failures(self):
        from mimo2api.user_import import import_users_batch

        result = import_users_batch(
            [
                {
                    "userId": "uid-a",
                    "serviceToken": "token-a",
                    "xiaomichatbot_ph": "ph-a",
                    "name": "Account A",
                },
                {"userId": "uid-b", "serviceToken": "token-b"},
            ],
            users_dir=self.users_dir,
        )

        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["failure_count"], 1)
        self.assertEqual(result["imported"][0]["userId"], "uid-a")
        self.assertEqual(result["failures"][0]["userId"], "uid-b")
        self.assertIn("xiaomichatbot_ph", result["failures"][0]["reason"])
        self.assertEqual(self.read_user("uid-a")["name"], "Account A")
        self.assertFalse((self.users_dir / "user_uid-b.json").exists())

    def test_batch_import_accepts_uid_mapping_and_does_not_overwrite_by_default(self):
        from mimo2api.user_import import import_users_batch

        self.users_dir.mkdir()
        (self.users_dir / "user_uid-a.json").write_text(
            json.dumps(
                {
                    "userId": "uid-a",
                    "serviceToken": "old-token",
                    "xiaomichatbot_ph": "old-ph",
                    "name": "Old",
                }
            ),
            encoding="utf-8",
        )

        result = import_users_batch(
            {
                "uid-a": {
                    "serviceToken": "new-token",
                    "xiaomichatbot_ph": "new-ph",
                    "name": "New",
                }
            },
            users_dir=self.users_dir,
        )

        self.assertEqual(result["success_count"], 0)
        self.assertEqual(result["failure_count"], 1)
        self.assertIn("already exists", result["failures"][0]["reason"])
        self.assertEqual(self.read_user("uid-a")["serviceToken"], "old-token")

    def test_batch_import_overwrites_only_when_explicit(self):
        from mimo2api.user_import import import_users_batch

        self.users_dir.mkdir()
        (self.users_dir / "user_uid-a.json").write_text(
            json.dumps(
                {
                    "userId": "uid-a",
                    "serviceToken": "old-token",
                    "xiaomichatbot_ph": "old-ph",
                    "name": "Old",
                }
            ),
            encoding="utf-8",
        )

        result = import_users_batch(
            {
                "overwrite": True,
                "users": {
                    "uid-a": {
                        "serviceToken": "new-token",
                        "xiaomichatbot_ph": "new-ph",
                        "name": "New",
                    }
                },
            },
            users_dir=self.users_dir,
        )

        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["failure_count"], 0)
        self.assertEqual(self.read_user("uid-a")["serviceToken"], "new-token")
        self.assertEqual(self.read_user("uid-a")["name"], "New")
