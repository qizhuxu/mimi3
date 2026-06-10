import io
import logging
import shutil
import tempfile
import unittest
from pathlib import Path


class LoggingUtilsTests(unittest.TestCase):
    def test_compact_text_truncates_single_line_with_fingerprint(self):
        from mimo2api.logging_utils import compact_text

        output = compact_text("line1\n" + ("x" * 200), limit=40)

        self.assertNotIn("\n", output)
        self.assertIn("\\n", output)
        self.assertIn("truncated_chars=", output)
        self.assertIn("sha1=", output)
        self.assertLess(len(output), 120)

    def test_log_event_emits_compact_key_value_message(self):
        from mimo2api.logging_utils import log_event

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("tests.logging_utils")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

        log_event(
            logger,
            logging.INFO,
            "bridge.inject.reply",
            uid="user 1",
            attempt=2,
            reply="ok\n" + ("z" * 100),
            text_limit=30,
        )

        message = stream.getvalue().strip()
        self.assertIn("event=bridge.inject.reply", message)
        self.assertIn('uid="user 1"', message)
        self.assertIn("attempt=2", message)
        self.assertIn("reply=", message)
        self.assertIn("truncated_chars=", message)
        self.assertNotIn("\n", message)

    def test_main_logging_config_disables_access_log_by_default(self):
        import main

        tmp = tempfile.mkdtemp()
        try:
            settings = main.configure_logging(log_dir=tmp, log_level="DEBUG", access_log=False)

            self.assertEqual(logging.getLogger().level, logging.DEBUG)
            self.assertFalse(settings.access_log_enabled)
            self.assertTrue(logging.getLogger("uvicorn.access").disabled)
            self.assertGreaterEqual(logging.getLogger("uvicorn.access").level, logging.WARNING)
        finally:
            root_logger = logging.getLogger()
            for handler in list(root_logger.handlers):
                root_logger.removeHandler(handler)
                handler.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_run_gateway_passes_shutdown_timeout_to_uvicorn(self):
        import main

        calls = []
        original_run = main.uvicorn.run
        original_configure = main.configure_logging
        try:
            main.configure_logging = lambda: main.LoggingSettings(
                log_level_name="INFO",
                log_level=logging.INFO,
                access_log_enabled=False,
                log_file=None,
            )
            main.uvicorn.run = lambda *args, **kwargs: calls.append((args, kwargs))

            main.run_gateway()
        finally:
            main.uvicorn.run = original_run
            main.configure_logging = original_configure

        self.assertEqual(calls[0][1]["timeout_graceful_shutdown"], 5)
        self.assertFalse(calls[0][1]["access_log"])


class ManagerLogNoiseTests(unittest.TestCase):
    def test_manager_does_not_log_full_ai_replies_or_reset_prompts_at_info(self):
        source = Path("mimo2api/manager.py").read_text(encoding="utf-8")

        self.assertNotIn("注入反馈 attempt {attempt}]: {reply}", source)
        self.assertNotIn("[{label}] [/reset 反馈]: {reset_reply}", source)
        self.assertNotIn("下发环境重置指令(soul.md): {reset_soul_cmd}", source)
        self.assertNotIn("下发环境重置指令(AGENTS.md): {reset_agents_cmd}", source)
        self.assertNotIn("[收到的 soul.md 重置反馈]: {reply_soul}", source)
        self.assertNotIn("[收到的 AGENTS.md 重置反馈]: {reply_agents}", source)
