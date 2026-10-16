from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPOSITORY_ROOT / "scripts" / "run-codebuddy-bill-payment.sh"


class CodeBuddyBillPaymentLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.project = self.root / "project"
        self.project.mkdir()
        subprocess.run(
            ["git", "init", "-b", "lz-stock-feature"],
            cwd=self.project,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "codebuddy-test@example.invalid"],
            cwd=self.project,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "CodeBuddy Launcher Test"],
            cwd=self.project,
            check=True,
        )
        (self.project / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.project, check=True)
        subprocess.run(
            ["git", "commit", "-m", "test fixture"],
            cwd=self.project,
            check=True,
            capture_output=True,
            text=True,
        )
        self.packet = self.root / "packet.md"
        self.packet.write_text("bounded bill payment prompt\n", encoding="utf-8")
        self.wrapper = self.root / "codebuddy"
        self.wrapper.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        self.wrapper.chmod(0o755)

    def run_launcher(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = {
            **os.environ,
            "DIMON_CODEBUDDY_PROJECT": str(self.project),
            "DIMON_CODEBUDDY_PACKET": str(self.packet),
            "DIMON_CODEBUDDY_WRAPPER": str(self.wrapper),
        }
        return subprocess.run(
            [str(LAUNCHER), *arguments],
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
        )

    def test_check_accepts_clean_expected_branch(self) -> None:
        completed = self.run_launcher("--check")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("READY", completed.stdout)
        self.assertIn("lz-stock-feature", completed.stdout)

    def test_check_rejects_dirty_worktree(self) -> None:
        (self.project / "README.md").write_text("changed\n", encoding="utf-8")

        completed = self.run_launcher("--check")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("tracked working tree must be clean", completed.stderr)

    def test_check_allows_an_unrelated_untracked_directory(self) -> None:
        unrelated = self.project / "parallel-work"
        unrelated.mkdir()
        (unrelated / "README.md").write_text("separate work\n", encoding="utf-8")

        completed = self.run_launcher("--check")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("READY", completed.stdout)

    def test_check_rejects_wrong_branch(self) -> None:
        subprocess.run(
            ["git", "switch", "-c", "wrong-branch"],
            cwd=self.project,
            check=True,
            capture_output=True,
            text=True,
        )

        completed = self.run_launcher("--check")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("must run on branch lz-stock-feature", completed.stderr)

    def test_print_prompt_outputs_exact_work_packet(self) -> None:
        completed = self.run_launcher("--print-prompt")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "bounded bill payment prompt\n")


if __name__ == "__main__":
    unittest.main()
