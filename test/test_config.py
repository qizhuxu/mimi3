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

    def test_prompt_store_derives_values_from_tunnel_config(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=True):
            project = Path(td)
            templates = project / "data" / "prompts" / "templates.json"
            templates.parent.mkdir(parents=True)
            templates.write_text(json.dumps({
                "templates": [
                    {
                        "prompt_id": "deploy.test",
                        "enabled": True,
                        "text": "{{PUBLIC_HOSTNAME}} {{LOCAL_PORT}} {{UPSTREAM}} {{API_KEY_ENV}}",
                        "preferred_after": [],
                    }
                ]
            }), encoding="utf-8")
            data_config = project / "data" / "config" / "config.json"
            data_config.parent.mkdir(parents=True)
            data_config.write_text(json.dumps({
                "tunnel": {
                    "public_hostname": "derived.example.com",
                    "local_port": 8365,
                    "upstream": "upstream.example.com:443",
                    "api_key_env": "MIMO_API_KEY",
                },
                "prompt_store": {"substitution_values": {}},
            }), encoding="utf-8")

            with self._patch_paths(project):
                store = PromptStore(templates)

            self.assertEqual(
                store.get("deploy.test").text,
                "derived.example.com 8365 upstream.example.com:443 MIMO_API_KEY",
            )

    def test_webui_port_env_overrides_data_config(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"WEBUI_PORT": "9460"}, clear=True):
            project = Path(td)
            data_config = project / "data" / "config" / "config.json"
            data_config.parent.mkdir(parents=True)
            data_config.write_text(json.dumps({"webui": {"port": 8358}}), encoding="utf-8")

            with self._patch_paths(project):
                loaded = config.load()

            self.assertEqual(loaded["webui"]["port"], 9460)

    def test_webui_port_can_come_from_data_config(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=True):
            project = Path(td)
            data_config = project / "data" / "config" / "config.json"
            data_config.parent.mkdir(parents=True)
            data_config.write_text(json.dumps({"webui": {"host": "127.0.0.1", "port": 9461}}), encoding="utf-8")

            with self._patch_paths(project):
                loaded = config.load()

            self.assertEqual(loaded["webui"]["host"], "127.0.0.1")
            self.assertEqual(loaded["webui"]["port"], 9461)


if __name__ == "__main__":
    unittest.main()
