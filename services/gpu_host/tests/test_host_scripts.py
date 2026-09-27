from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.gpu_host.host_cli import collect_health, runtime_directory


ROOT = Path(__file__).resolve().parents[3]


class HostScriptTests(unittest.TestCase):
    def test_shell_entry_points_are_strict_thin_wrappers(self) -> None:
        for name in ("setup-host", "start-host", "stop-host", "healthcheck"):
            path = ROOT / "scripts" / name
            body = path.read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertTrue(os.access(path, os.X_OK))
                self.assertIn("set -euo pipefail", body)
                self.assertIn("services.gpu_host.host_cli", body)
                self.assertNotIn("0.0.0.0", body)
                subprocess.run(["bash", "-n", str(path)], check=True)

    def test_modes_and_download_are_explicit(self) -> None:
        for mode in ("shared", "standalone"):
            result = subprocess.run(
                [str(ROOT / "scripts" / "setup-host"), "--mode", mode, "--help"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("--download", result.stdout)
            self.assertIn("--accept-licenses", result.stdout)

    def test_runtime_state_is_scoped_under_xdg_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = runtime_directory({"XDG_RUNTIME_DIR": temp})
            self.assertEqual(path, Path(temp) / "dimon-gateway")

    def test_health_is_safe_typed_and_reports_revision_agreement(self) -> None:
        env = {
            "DIMON_QWEN_REVISION": "9f646d7eda193ddf2348134f3bff3d49eed7a2c6",
            "DIMON_NEMOTRON_REVISION": "ebe59e5a817142986528bbbee5dba8db7b38ed50",
            "DIMON_KOKORO_REVISION": "f3ff3571791e39611d31c381e3a41a3af07b4987",
        }
        with patch(
            "services.gpu_host.host_cli.probe_tcp",
            side_effect=lambda host, port, timeout=1: port in {8080, 8004, 3001},
        ), patch(
            "services.gpu_host.host_cli.gpu_free_mb", return_value=4096
        ):
            report = collect_health(env)
        encoded = json.dumps(report)
        self.assertEqual(report["status"], "not_ready")
        self.assertEqual(report["services"]["gateway"]["reason_code"], "UNREACHABLE")
        self.assertTrue(report["revision_agreement"])
        self.assertNotIn("path", encoded.lower())


if __name__ == "__main__":
    unittest.main()
