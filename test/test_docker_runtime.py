import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DockerRuntimeTests(unittest.TestCase):
    def test_dockerfile_keeps_unmounted_prompt_template_seed(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("COPY data/prompts ./data/prompts", dockerfile)
        self.assertIn(
            "COPY data/prompts/templates.json ./defaults/prompts/templates.json",
            dockerfile,
        )


if __name__ == "__main__":
    unittest.main()
