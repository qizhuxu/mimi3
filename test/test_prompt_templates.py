import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_PATH = ROOT / "data" / "prompts" / "templates.json"


class DeployPromptTemplateTests(unittest.TestCase):
    def _template_text(self, prompt_id: str) -> str:
        data = json.loads(TEMPLATES_PATH.read_text(encoding="utf-8"))
        for template in data["templates"]:
            if template["prompt_id"] == prompt_id:
                return template["text"]
        raise AssertionError(f"template not found: {prompt_id}")

    def test_standard_prompt_requires_openai_compatibility_shim_verification(self):
        text = self._template_text("deploy.v1.standard")

        self.assertIn("OpenAI 兼容 shim", text)
        self.assertIn("pgrep -f openai_compat_proxy.py", text)
        self.assertIn('"[undefined]"', text)
        self.assertIn("/v1/chat/completions", text)

    def test_retry_prompt_requires_openai_compatibility_shim_verification(self):
        text = self._template_text("deploy.v1.no_install_retry")

        self.assertIn("OpenAI 兼容 shim", text)
        self.assertIn("pgrep -f openai_compat_proxy.py", text)
        self.assertIn('"[undefined]"', text)
        self.assertIn("/v1/chat/completions", text)


if __name__ == "__main__":
    unittest.main()
