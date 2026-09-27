from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.repository_boundary import (
    content_violations,
    path_violations,
    publication_candidates,
)


class PathViolationTests(unittest.TestCase):
    def test_rejects_private_or_generated_paths(self) -> None:
        rejected = {
            "apps/dimon_live/.env": "active environment file",
            "apps/dimon_live/data/demo.sqlite3": "database",
            "runtime/ledgers/visitor.sqlite3": "runtime data",
            "models/qwen.gguf": "model artifact",
            "models/qwen.safetensors": "model artifact",
            "models/qwen.bin": "model artifact",
            "models/qwen.onnx": "model artifact",
            "runtime/audio/result.wav": "runtime data",
            "apps/dimon_live/__pycache__/app.pyc": "cache",
            "deploy/cloudflare/cert.pem": "Cloudflare credential",
            "deploy/cloudflare/abc123.json": "Cloudflare credential",
            "docs/competition handbook/tencent hackathon brief.pdf": "competition PDF",
        }

        for path, expected_reason in rejected.items():
            with self.subTest(path=path):
                reasons = path_violations(Path(path))
                self.assertTrue(
                    any(expected_reason.lower() in reason.lower() for reason in reasons),
                    reasons,
                )

    def test_allows_examples_and_source_files(self) -> None:
        for path in (
            ".env.example",
            "apps/dimon_live/.env.example",
            "fixtures/inference/manifest.json",
            "deploy/cloudflare/access-policy.example.json",
            "apps/dimon_live/app.py",
        ):
            with self.subTest(path=path):
                self.assertEqual(path_violations(Path(path)), [])


class ContentViolationTests(unittest.TestCase):
    def test_rejects_personal_paths_and_secret_values(self) -> None:
        samples = {
            "model=/" + "home/alice/models/qwen.gguf": "personal absolute path",
            "CF_ACCESS_CLIENT_SECRET=" + "cf" + "ast_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN12345678": "credential",
            "api_key=" + "s" + "k-abcdefghijklmnopqrstuvwxyz012345": "credential",
            "-----BEGIN " + "PRIVATE KEY-----": "private key",
        }

        for content, expected_reason in samples.items():
            with self.subTest(content=content):
                reasons = content_violations(content)
                self.assertTrue(
                    any(expected_reason.lower() in reason.lower() for reason in reasons),
                    reasons,
                )

    def test_allows_documented_placeholders(self) -> None:
        content = "\n".join(
            (
                "model=${QWEN_MODEL_PATH}",
                "home=/home/<user>/models",
                "CF_ACCESS_CLIENT_SECRET=<replace-me>",
                "api_key=example-only",
            )
        )
        self.assertEqual(content_violations(content), [])


class CandidateSelectionTests(unittest.TestCase):
    def test_filesystem_mode_honors_ignore_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".gitignore").write_text(".env\nruntime/\n", encoding="utf-8")
            (root / "README.md").write_text("safe\n", encoding="utf-8")
            (root / ".env").write_text("SECRET=value\n", encoding="utf-8")
            (root / "runtime").mkdir()
            (root / "runtime" / "state.sqlite3").write_bytes(b"sqlite")

            self.assertEqual(
                publication_candidates(root, prefer_git=False),
                [Path(".gitignore"), Path("README.md")],
            )


class ScriptContractTests(unittest.TestCase):
    def test_script_rejects_a_forbidden_candidate_without_echoing_secret(self) -> None:
        script = Path(__file__).parents[1] / "scripts" / "check-repository-boundary"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "leaked.env").write_text(
                "CF_ACCESS_CLIENT_SECRET="
                + "cf"
                + "ast_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN12345678\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [sys.executable, str(script), "--root", str(root), "--filesystem"],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("leaked.env", completed.stdout)
        self.assertNotIn("cfast_", completed.stdout)


if __name__ == "__main__":
    unittest.main()
