import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import config
from src import webui_runtime


class WebuiRuntimeTests(unittest.TestCase):
    def tearDown(self):
        config.reload()

    def test_resolve_webui_bind_reads_config_file(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=True):
            project = Path(td)
            data_config = project / "data" / "config" / "config.json"
            data_config.parent.mkdir(parents=True)
            data_config.write_text(
                json.dumps({"webui": {"host": "127.0.0.1", "port": 9462}}),
                encoding="utf-8",
            )
            with patch.multiple(
                config,
                _PROJECT=project,
                CONFIG_FILE=data_config,
                LEGACY_CONFIG_FILE=project / "config.json",
            ):
                config.reload()
                self.assertEqual(webui_runtime.resolve_webui_bind(), ("127.0.0.1", 9462))

    def test_resolve_webui_bind_uses_env_priority(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ,
            {"WEBUI_HOST": "0.0.0.0", "WEBUI_PORT": "9463"},
            clear=True,
        ):
            project = Path(td)
            data_config = project / "data" / "config" / "config.json"
            data_config.parent.mkdir(parents=True)
            data_config.write_text(
                json.dumps({"webui": {"host": "127.0.0.1", "port": 8358}}),
                encoding="utf-8",
            )
            with patch.multiple(
                config,
                _PROJECT=project,
                CONFIG_FILE=data_config,
                LEGACY_CONFIG_FILE=project / "config.json",
            ):
                config.reload()
                self.assertEqual(webui_runtime.resolve_webui_bind(), ("0.0.0.0", 9463))


if __name__ == "__main__":
    unittest.main()
