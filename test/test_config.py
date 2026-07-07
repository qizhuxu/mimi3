import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import config
from src.prompt_store import PromptStore


class ConfigPathTests(unittest.TestCase):
    def tearDown(self):
        config.reload()

    def _patch_paths(self, project: Path):
        data_config = project / "data" / "config" / "config.json"
        legacy_config = project / "config.json"
        return patch.multiple(
            config,
            _PROJECT=project,
            CONFIG_FILE=data_config,
            LEGACY_CONFIG_FILE=legacy_config,
        )

    def test_load_prefers_data_config_over_legacy_root_config(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=True):
            project = Path(td)
            data_config = project / "data" / "config" / "config.json"
            data_config.parent.mkdir(parents=True)
            data_config.write_text(json.dumps({"pool": {"min_accounts": 11}}), encoding="utf-8")
            (project / "config.json").write_text(json.dumps({"pool": {"min_accounts": 3}}), encoding="utf-8")

            with self._patch_paths(project):
                loaded = config.load()

            self.assertEqual(loaded["pool"]["min_accounts"], 11)

    def test_load_copies_legacy_root_config_to_data_config(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=True):
            project = Path(td)
            legacy = project / "config.json"
            legacy.write_text(json.dumps({"pool": {"min_accounts": 12}}), encoding="utf-8")

            with self._patch_paths(project):
                loaded = config.load()
                data_config = config.CONFIG_FILE

            self.assertEqual(loaded["pool"]["min_accounts"], 12)
            self.assertTrue(data_config.exists())
            self.assertEqual(json.loads(data_config.read_text(encoding="utf-8"))["pool"]["min_accounts"], 12)

    def test_prompt_store_uses_data_config_substitution_values(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=True):
            project = Path(td)
            templates = project / "data" / "prompts" / "templates.json"
            templates.parent.mkdir(parents=True)
            templates.write_text(json.dumps({
                "templates": [
                    {
                        "prompt_id": "deploy.test",
                        "enabled": True,
                        "text": "host={{PUBLIC_HOSTNAME}}",
                        "preferred_after": [],
                    }
                ]
            }), encoding="utf-8")
            data_config = project / "data" / "config" / "config.json"
            data_config.parent.mkdir(parents=True)
            data_config.write_text(json.dumps({
                "prompt_store": {
                    "substitution_values": {
                        "PUBLIC_HOSTNAME": "mimo.test.local",
                    }
                }
            }), encoding="utf-8")

            with self._patch_paths(project):
                store = PromptStore(templates)

            self.assertEqual(store.get("deploy.test").text, "host=mimo.test.local")


if __name__ == "__main__":
    unittest.main()
