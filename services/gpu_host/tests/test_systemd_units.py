from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DEPLOY = ROOT / "deploy" / "systemd"


class SystemdUnitTests(unittest.TestCase):
    def test_templates_have_ordering_bounded_restarts_and_no_credentials(self) -> None:
        host = (DEPLOY / "dimon-gpu-host.service").read_text(encoding="utf-8")
        gateway = (DEPLOY / "dimon-inference-gateway.service").read_text(encoding="utf-8")
        for body in (host, gateway):
            self.assertIn("WorkingDirectory=@REPO_ROOT@", body)
            self.assertIn("EnvironmentFile=@ENV_FILE@", body)
            self.assertIn("Restart=on-failure", body)
            self.assertIn("RestartSec=5", body)
            self.assertIn("StartLimitBurst=3", body)
            self.assertNotIn("CF-Access-Client", body)
            self.assertNotIn("stocksuite.app", body)
        self.assertIn("--components raw", host)
        self.assertIn("After=dimon-gpu-host.service", gateway)
        self.assertIn("Requires=dimon-gpu-host.service", gateway)
        self.assertIn("services/inference_gateway/run.sh", gateway)

    def test_installer_renders_absolute_paths_without_enabling_units(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "units"
            env_file = Path(temp) / "host.env"
            env_file.write_text("DIMON_GATEWAY_HOST=127.0.0.1\n", encoding="utf-8")
            env_file.chmod(0o600)
            result = subprocess.run(
                [
                    str(ROOT / "scripts" / "install-user-services"),
                    "--destination",
                    str(destination),
                    "--env-file",
                    str(env_file),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            rendered = (destination / "dimon-gpu-host.service").read_text(encoding="utf-8")
            self.assertIn(str(ROOT), rendered)
            self.assertIn(str(env_file), rendered)
            self.assertNotIn("@REPO_ROOT@", rendered)
            self.assertNotIn("systemctl", result.stdout + result.stderr)

    def test_documentation_requires_private_environment_file(self) -> None:
        readme = (DEPLOY / "README.md").read_text(encoding="utf-8")
        self.assertIn("chmod 600", readme)
        self.assertIn("daemon-reload", readme)
        self.assertIn("journalctl", readme)
        self.assertIn("rollback", readme.lower())


if __name__ == "__main__":
    unittest.main()
