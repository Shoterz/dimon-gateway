from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class CleanCloneTests(unittest.TestCase):
    def test_committed_tree_clones_without_private_or_runtime_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            clone = Path(temp_dir) / "dimon-gateway"
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--quiet",
                    "--local",
                    "--no-hardlinks",
                    str(ROOT),
                    str(clone),
                ],
                check=True,
            )
            tracked = set(
                subprocess.check_output(
                    ["git", "-C", str(clone), "ls-files"],
                    text=True,
                ).splitlines()
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/check-repository-boundary"),
                    "--root",
                    str(clone),
                    "--committed",
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("committed file(s)", completed.stdout)
        self.assertNotIn("apps/dimon_live/.env", tracked)
        self.assertNotIn("apps/dimon_live/data/demo.sqlite3", tracked)
        self.assertFalse(any(path.startswith("runtime/") for path in tracked))
        self.assertFalse(any(path.endswith(".pdf") for path in tracked))


if __name__ == "__main__":
    unittest.main()
