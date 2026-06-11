import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path


class HfSpaceDeployTests(unittest.TestCase):
    ENV_KEYS = (
        "MIMO_DEPLOY_TARGET",
        "SERVER_PORT",
        "MIMO_TUNNEL_MODE",
        "MIMO_CLOUDFLARED_BIN",
        "MIMO_RUNTIME_CONFIG_PATH",
        "MIMO_CONFIG_SOURCE",
        "MIMO_TUNNEL_ACTIVE_WS_URL",
        "MIMO2API_WS_URL",
    )

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.original_env = {key: os.environ.get(key) for key in self.ENV_KEYS}
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["MIMO_RUNTIME_CONFIG_PATH"] = str(self.tmp / "runtime_config.json")

        import mimo2api.runtime_config as runtime_config

        self.runtime_config = runtime_config
        runtime_config.reload_runtime_config()

    def tearDown(self):
        for key, value in self.original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.runtime_config.reload_runtime_config()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def enable_hf_space(self):
        os.environ["MIMO_DEPLOY_TARGET"] = "hf_space"
        self.runtime_config.reload_runtime_config()

    def test_hf_space_defaults_to_port_7860_and_marks_tunnel_unavailable(self):
        self.enable_hf_space()

        metadata = self.runtime_config.get_config_metadata()

        self.assertEqual(self.runtime_config.get_config_value("server.port"), 7860)
        self.assertEqual(metadata["_meta"]["deploy_target"], "hf_space")
        self.assertFalse(metadata["_meta"]["features"]["tunnel"])
        self.assertEqual(metadata["tunnel.mode"]["value"], "none")
        self.assertEqual(metadata["tunnel.mode"]["source"], "deployment")
        self.assertFalse(metadata["tunnel.mode"]["editable"])
        self.assertTrue(metadata["tunnel.mode"]["hidden"])

    def test_server_port_env_can_override_hf_space_default(self):
        os.environ["MIMO_DEPLOY_TARGET"] = "hf_space"
        os.environ["SERVER_PORT"] = "9000"
        self.runtime_config.reload_runtime_config()

        self.assertEqual(self.runtime_config.get_config_value("server.port"), 9000)

    def test_hf_space_forces_cloudflare_tunnel_off_even_when_env_requests_it(self):
        os.environ["MIMO_DEPLOY_TARGET"] = "hf_space"
        os.environ["MIMO_TUNNEL_MODE"] = "cloudflare_quick"
        self.runtime_config.reload_runtime_config()

        result = self.runtime_config.update_runtime_config({"tunnel.mode": "cloudflare_named"})

        self.assertEqual(self.runtime_config.get_config_value("tunnel.mode"), "none")
        self.assertNotIn("tunnel.mode", result["changed"])

    def test_tunnel_supervisor_does_not_spawn_cloudflared_in_hf_space(self):
        self.enable_hf_space()
        os.environ["MIMO_TUNNEL_MODE"] = "cloudflare_quick"

        import mimo2api.tunnel_supervisor as tunnel_supervisor

        async def fail_if_called(*args, **kwargs):
            raise AssertionError("cloudflared must not start in hf_space mode")

        original_create_subprocess_exec = asyncio.create_subprocess_exec
        asyncio.create_subprocess_exec = fail_if_called
        try:
            supervisor = tunnel_supervisor.TunnelSupervisor()
            asyncio.run(supervisor.start())
            snapshot = supervisor.snapshot()
        finally:
            asyncio.create_subprocess_exec = original_create_subprocess_exec

        self.assertIsNone(supervisor.process)
        self.assertEqual(snapshot["mode"], "none")
        self.assertFalse(snapshot["available"])
        self.assertEqual(snapshot["disabled_reason"], "hf_space")

    def test_webui_contains_tunnel_feature_gate(self):
        html = Path("mimo2api/webui.html").read_text(encoding="utf-8")

        self.assertIn('data-feature="tunnel"', html)
        self.assertIn("function isTunnelAvailable()", html)
        self.assertIn("function applyFeatureVisibility()", html)
        self.assertIn("item.hidden", html)

    def test_container_files_use_hf_space_defaults(self):
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        compose = Path("docker-compose.yml").read_text(encoding="utf-8")
        env_example = Path("env.example").read_text(encoding="utf-8")
        hf_doc = Path("HUGGINGFACE_SPACE.md").read_text(encoding="utf-8")

        self.assertIn("MIMO_DEPLOY_TARGET=hf_space", dockerfile)
        self.assertIn("SERVER_PORT=7860", dockerfile)
        self.assertIn("EXPOSE 7860", dockerfile)
        self.assertIn("image: mimi3:hf_latest", compose)
        self.assertIn("${SERVER_PORT:-7860}:${SERVER_PORT:-7860}", compose)
        self.assertIn("MIMO_DEPLOY_TARGET=hf_space", env_example)
        self.assertIn("SERVER_PORT=7860", env_example)
        self.assertIn("WS_TUNNEL_URL=wss://your-space-host/ws", env_example)
        self.assertIn("mimi3:hf_latest", hf_doc)
        self.assertIn("wss://<your-space-host>/ws", hf_doc)
