from __future__ import annotations

import json
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
        medium = (DEPLOY / "dimon-faster-whisper-medium.service").read_text(
            encoding="utf-8"
        )
        for body in (host, gateway, medium):
            self.assertIn("WorkingDirectory=@REPO_ROOT@", body)
            self.assertIn("EnvironmentFile=@ENV_FILE@", body)
            self.assertIn("Restart=on-failure", body)
            self.assertIn("RestartSec=5", body)
            self.assertIn("StartLimitBurst=3", body)
            self.assertNotIn("CF-Access-Client", body)
            self.assertNotIn("stocksuite.app", body)
        self.assertIn("--components raw", host)
        self.assertNotIn("dimon-gpu-host.service", gateway)
        self.assertIn("services/inference_gateway/run.sh", gateway)
        self.assertIn("After=gpu-overlord-stack.service", medium)
        self.assertNotIn("After=dimon-inference-gateway.service", medium)
        self.assertIn("services/faster_whisper_cuda/run.sh", medium)
        self.assertIn("TimeoutStopSec=30", medium)

    def test_gateway_reset_dropin_keeps_control_plane_alive(self) -> None:
        host_upstreams = (
            DEPLOY / "dimon-inference-gateway-host-upstreams.conf"
        ).read_text(encoding="utf-8")

        self.assertIn("Requires=jarvis-whisper-cpu.service", host_upstreams)
        self.assertIn("Wants=gpu-overlord-stack.service", host_upstreams)
        self.assertNotIn("Requires=gpu-overlord-stack.service", host_upstreams)

    def test_dimon_gpu_budget_keeps_all_four_resident_profiles_admissible(self) -> None:
        config = json.loads(
            (ROOT / "deploy/gpu/gpu_overlord_dimon.json").read_text(
                encoding="utf-8"
            )
        )
        profiles = config["profiles_mb"]
        resident_mb = sum(
            (
                profiles["llama"]["qwen35-9b-abliterated-q5-4k"],
                profiles["nemotron"]["nemotron-en"],
                profiles["kokoro"]["kokoro-82m"],
                profiles["faster_whisper"]["faster-whisper-medium-cuda"],
                config["safety_reserve_mb"],
            )
        )

        self.assertEqual(profiles["kokoro"]["kokoro-82m"], 1280)
        self.assertLessEqual(resident_mb, 11_520)
        dropin = (DEPLOY / "gpu-overlord-stack-dimon.conf").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "GPU_OVERLORD_CONFIG=@REPO_ROOT@/deploy/gpu/",
            dropin,
        )

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
            medium = (destination / "dimon-faster-whisper-medium.service").read_text(
                encoding="utf-8"
            )
            self.assertIn(str(ROOT), rendered)
            self.assertIn(str(env_file), rendered)
            self.assertNotIn("@REPO_ROOT@", rendered)
            self.assertIn(str(ROOT), medium)
            self.assertIn(str(env_file), medium)
            self.assertNotIn("@REPO_ROOT@", medium)
            gateway_dropin = (
                destination
                / "dimon-inference-gateway.service.d"
                / "host-upstreams.conf"
            ).read_text(encoding="utf-8")
            overlord_dropin = (
                destination / "gpu-overlord-stack.service.d" / "dimon.conf"
            ).read_text(encoding="utf-8")
            self.assertIn("Requires=jarvis-whisper-cpu.service", gateway_dropin)
            self.assertIn(str(ROOT), overlord_dropin)
            self.assertNotIn("@REPO_ROOT@", overlord_dropin)
            self.assertIn("rendered 5 systemd files", result.stdout)
            self.assertNotIn("systemctl", result.stdout + result.stderr)

    def test_documentation_requires_private_environment_file(self) -> None:
        readme = (DEPLOY / "README.md").read_text(encoding="utf-8")
        self.assertIn("chmod 600", readme)
        self.assertIn("daemon-reload", readme)
        self.assertIn("journalctl", readme)
        self.assertIn("rollback", readme.lower())
        self.assertIn(".runtime/faster-whisper-medium", readme)
        self.assertIn("127.0.0.1:8007", readme)


if __name__ == "__main__":
    unittest.main()
