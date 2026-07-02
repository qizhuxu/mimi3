import os
import shutil
import tempfile
import unittest
from pathlib import Path


class BridgePromptStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.path = Path(self.tmp_dir) / "bridge_prompt_templates.json"
        self.original_path = os.environ.get("MIMO_BRIDGE_PROMPT_TEMPLATES_PATH")
        os.environ["MIMO_BRIDGE_PROMPT_TEMPLATES_PATH"] = str(self.path)

    def tearDown(self):
        if self.original_path is None:
            os.environ.pop("MIMO_BRIDGE_PROMPT_TEMPLATES_PATH", None)
        else:
            os.environ["MIMO_BRIDGE_PROMPT_TEMPLATES_PATH"] = self.original_path
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_default_templates_have_required_fields_and_render_bridge_code(self):
        from mimo2api.bridge_prompt_store import default_bridge_prompt_templates, render_bridge_prompt_text

        templates = default_bridge_prompt_templates()

        self.assertEqual(len(templates), 5)
        self.assertEqual(
            [template.prompt_id for template in templates],
            [
                "bridge.v1.standard",
                "bridge.v1.existing_deps",
                "bridge.v1.no_install_after_dependency_refusal",
                "bridge.v1.connector_scope",
                "bridge.v1.relay_ready_self_check",
            ],
        )
        for template in templates:
            with self.subTest(prompt_id=template.prompt_id):
                self.assertTrue(template.prompt_id)
                self.assertTrue(template.name)
                self.assertTrue(template.text)
                self.assertIsInstance(template.enabled, bool)
                self.assertIsInstance(template.preferred_after, tuple)
                self.assertIsInstance(template.description, str)
                self.assertTrue(template.updated_at)

        rendered = render_bridge_prompt_text(templates[0], "BRIDGE_CODE")
        self.assertIn("BRIDGE_CODE", rendered)
        self.assertNotIn("{bridge_code}", rendered)

    def test_default_templates_make_v1_dependencies_explicit(self):
        from mimo2api.bridge_prompt_store import default_bridge_prompt_templates

        templates = {template.prompt_id: template for template in default_bridge_prompt_templates()}

        self.assertIn("Bridge v1 运行前置条件", templates["bridge.v1.standard"].text)
        self.assertIn("DEPENDENCY_MISSING", templates["bridge.v1.standard"].text)
        self.assertIn("STARTED_BRIDGE_V1", templates["bridge.v1.standard"].text)
        self.assertIn("{bridge_code}", templates["bridge.v1.standard"].text)

        unavailable = templates["bridge.v1.no_install_after_dependency_refusal"]
        self.assertIn("DEPENDENCY_UNAVAILABLE", unavailable.text)
        self.assertIn("STARTED_BRIDGE_V1", unavailable.text)
        self.assertIn("缺失时不要运行 bridge v1", unavailable.text)
        self.assertIn("{bridge_code}", unavailable.text)

        connector = templates["bridge.v1.connector_scope"]
        self.assertIn("Bridge v1 仍然需要 websockets/httpx", connector.text)
        self.assertIn("DEPENDENCY_MISSING", connector.text)

    def test_save_load_and_reset_template_library(self):
        from mimo2api.bridge_prompt_store import (
            load_bridge_prompt_templates,
            reset_bridge_prompt_templates,
            save_bridge_prompt_templates,
        )

        result = save_bridge_prompt_templates(
            [
                {
                    "prompt_id": "custom.alpha",
                    "name": "Alpha",
                    "enabled": True,
                    "text": "run {bridge_code}",
                    "preferred_after": ["timeout"],
                    "description": "custom template",
                }
            ]
        )

        self.assertTrue(result["ok"])
        loaded = load_bridge_prompt_templates()
        self.assertEqual([template.prompt_id for template in loaded], ["custom.alpha"])
        self.assertTrue(loaded[0].updated_at)

        reset_result = reset_bridge_prompt_templates()
        self.assertTrue(reset_result["ok"])
        reset_ids = {template.prompt_id for template in load_bridge_prompt_templates()}
        self.assertIn("bridge.v1.standard", reset_ids)
        self.assertNotIn("custom.alpha", reset_ids)

    def test_validation_rejects_duplicate_empty_and_unknown_failure_classes(self):
        from mimo2api.bridge_prompt_store import save_bridge_prompt_templates

        valid = {
            "prompt_id": "custom.alpha",
            "name": "Alpha",
            "enabled": True,
            "text": "run {bridge_code}",
            "preferred_after": [],
            "description": "",
        }

        with self.assertRaises(ValueError):
            save_bridge_prompt_templates([valid, {**valid, "name": "Duplicate"}])

        with self.assertRaises(ValueError):
            save_bridge_prompt_templates([{**valid, "prompt_id": "custom.empty", "text": "  "}])

        with self.assertRaises(ValueError):
            save_bridge_prompt_templates([
                {**valid, "prompt_id": "custom.bad_failure", "preferred_after": ["not_a_failure_class"]}
            ])

    def test_manager_uses_configured_enabled_templates_and_skips_disabled(self):
        from mimo2api.bridge_prompt_store import save_bridge_prompt_templates
        from mimo2api.manager import build_effective_bridge_injection_prompt_library

        save_bridge_prompt_templates(
            [
                {
                    "prompt_id": "custom.disabled",
                    "name": "Disabled",
                    "enabled": False,
                    "text": "disabled {bridge_code}",
                    "preferred_after": [],
                    "description": "",
                },
                {
                    "prompt_id": "custom.enabled",
                    "name": "Enabled",
                    "enabled": True,
                    "text": "enabled {bridge_code}",
                    "preferred_after": ["timeout"],
                    "description": "",
                },
            ]
        )

        prompts = build_effective_bridge_injection_prompt_library("BRIDGE_CODE")

        self.assertEqual([prompt.prompt_id for prompt in prompts], ["custom.enabled"])
        self.assertEqual(prompts[0].preferred_after, ("timeout",))
        self.assertIn("BRIDGE_CODE", prompts[0].text)
        self.assertNotIn("custom.disabled", [prompt.prompt_id for prompt in prompts])

    def test_template_log_summary_redacts_secret_values_and_truncates_text(self):
        from mimo2api.bridge_prompt_store import summarize_bridge_prompt_templates

        summary = summarize_bridge_prompt_templates(
            [
                {
                    "prompt_id": "custom.secret",
                    "name": "Secret",
                    "enabled": True,
                    "text": "MIMO_API_KEY=sk-secret-value " + ("x" * 800),
                    "preferred_after": [],
                    "description": "",
                }
            ]
        )

        self.assertIn("custom.secret", summary)
        self.assertIn("sha1=", summary)
        self.assertIn("truncated_chars=", summary)
        self.assertNotIn("sk-secret-value", summary)
        self.assertNotIn("x" * 200, summary)
