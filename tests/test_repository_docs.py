from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class RepositoryDocumentationTests(unittest.TestCase):
    def test_readme_contains_the_locked_quick_start(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("Python 3.12", readme)
        self.assertIn("uv sync --locked --group dev", readme)
        self.assertIn("uv run dimon-serve", readme)
        self.assertIn("fixture", readme.lower())

    def test_readme_explains_the_model_and_security_boundary(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertRegex(readme, r"(?i)weights.*outside Git")
        self.assertIn("127.0.0.1:8080", readme)
        self.assertIn("127.0.0.1:8004", readme)
        self.assertIn("127.0.0.1:3001", readme)
        self.assertRegex(readme, r"(?i)raw model ports.*private")
        self.assertRegex(readme, r"(?i)SQLite.*local")

    def test_environment_example_names_all_modes_without_secrets(self) -> None:
        example = (ROOT / ".env.example").read_text(encoding="utf-8")

        self.assertIn("DIMON_INFERENCE_MODE=fixture", example)
        for mode in ("fixture", "remote", "local"):
            self.assertIn(mode, example)
        self.assertIn("DIMON_INFERENCE_GATEWAY_URL", example)
        self.assertIn("DIMON_CF_ACCESS_CLIENT_SECRET=", example)
        self.assertNotRegex(example, r"/home/[A-Za-z0-9._-]+/")
        self.assertNotRegex(example, r"cfast_[A-Za-z0-9]{20,}")

    def test_notice_separates_project_code_from_external_artifacts(self) -> None:
        notice = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")

        for name in ("Qwen", "Nemotron", "NeMo", "Kokoro", "llama.cpp"):
            self.assertIn(name, notice)
        self.assertRegex(notice, r"(?i)not redistributed")
        self.assertRegex(notice, r"https://")

    def test_license_is_mit_for_repository_owned_code(self) -> None:
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")

        self.assertIn("MIT License", license_text)
        self.assertIn("Dimon Gateway contributors", license_text)

    def test_app_local_example_points_to_the_canonical_template(self) -> None:
        local_example = (ROOT / "apps/dimon_live/.env.example").read_text(
            encoding="utf-8"
        )

        self.assertIn("../../.env.example", local_example)
        self.assertIn("cp .env.example apps/dimon_live/.env", local_example)


if __name__ == "__main__":
    unittest.main()
