from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class LauncherContractTests(unittest.TestCase):
    def launcher(self, service: str) -> tuple[Path, str]:
        path = ROOT / "services" / service / "run.sh"
        return path, path.read_text(encoding="utf-8")

    def test_every_launcher_is_strict_loopback_fixed_and_execs(self) -> None:
        expected = {
            "qwen": "8080",
            "nemotron": "8004",
            "kokoro": "3001",
            "faster_whisper_cuda": "8007",
        }
        for service, port in expected.items():
            path, body = self.launcher(service)
            with self.subTest(service=service):
                self.assertTrue(os.access(path, os.X_OK))
                self.assertIn("set -euo pipefail", body)
                self.assertIn("127.0.0.1", body)
                self.assertIn(port, body)
                self.assertIn("exec ", body)
                self.assertNotIn("0.0.0.0", body)
                self.assertNotIn("/home/", body)
                self.assertNotIn("EXTRA_ARGS", body)
                subprocess.run(["bash", "-n", str(path)], check=True)

    def test_launchers_require_fixed_artifact_and_runtime_variables(self) -> None:
        expectations = {
            "qwen": ("DIMON_QWEN_MODEL_PATH", "DIMON_LLAMA_SERVER_PATH", "--ctx-size"),
            "nemotron": ("DIMON_NEMOTRON_MODEL_PATH", "DIMON_NEMO_SPEECH_PATH", "nemotron-en"),
            "kokoro": ("DIMON_KOKORO_MODEL_PATH", "DIMON_KOKORO_CONFIG_PATH", "DIMON_KOKORO_VOICE_PATH"),
            "faster_whisper_cuda": (
                "DIMON_FASTER_WHISPER_PYTHON",
                "DIMON_FASTER_WHISPER_MODEL_PATH",
                "DIMON_GPU_OVERLORD_CLIENT_DIR",
            ),
        }
        for service, tokens in expectations.items():
            _, body = self.launcher(service)
            with self.subTest(service=service):
                for token in tokens:
                    self.assertIn(token, body)

    def test_check_mode_validates_without_starting_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "runtime"
            artifact = root / "model.bin"
            config = root / "config.json"
            voice = root / "voice.pt"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            artifact.write_bytes(b"model")
            config.write_text("{}", encoding="utf-8")
            voice.write_bytes(b"voice")
            environments = {
                "qwen": {
                    "DIMON_QWEN_MODEL_PATH": str(artifact),
                    "DIMON_LLAMA_SERVER_PATH": str(executable),
                },
                "nemotron": {
                    "DIMON_NEMOTRON_MODEL_PATH": str(artifact),
                    "DIMON_NEMO_SPEECH_PATH": str(executable),
                    "DIMON_NEMO_SPEECH_MODEL_DIR": str(root),
                },
                "kokoro": {
                    "DIMON_KOKORO_MODEL_PATH": str(artifact),
                    "DIMON_KOKORO_CONFIG_PATH": str(config),
                    "DIMON_KOKORO_VOICE_PATH": str(voice),
                    "DIMON_KOKORO_PYTHON": str(executable),
                },
            }
            for service, additions in environments.items():
                env = os.environ.copy()
                env.update(additions)
                result = subprocess.run(
                    [str(ROOT / "services" / service / "run.sh"), "--check"],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                with self.subTest(service=service):
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("READY", result.stdout)
                    self.assertNotIn(temp, result.stdout)

    def test_unknown_arguments_are_rejected(self) -> None:
        for service in ("qwen", "nemotron", "kokoro", "faster_whisper_cuda"):
            result = subprocess.run(
                [str(ROOT / "services" / service / "run.sh"), "--shell-fragment"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            with self.subTest(service=service):
                self.assertEqual(result.returncode, 2)

    def test_cuda_medium_check_requires_repository_owned_runtime_and_model(self) -> None:
        runtime_root = ROOT / ".runtime"
        runtime_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runtime_root) as temp, tempfile.TemporaryDirectory() as external:
            owned = Path(temp)
            python = owned / "venv/bin/python"
            model = owned / "model"
            client = Path(external)
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            python.chmod(0o755)
            model.mkdir()
            (model / "model.bin").write_bytes(b"model")
            (model / ".dimon-artifact.json").write_text("{}", encoding="utf-8")
            (client / "gpu_overlord_client.py").write_text("", encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "DIMON_PROJECT_PYTHON": str(python),
                    "DIMON_FASTER_WHISPER_PYTHON": str(python),
                    "DIMON_FASTER_WHISPER_MODEL_PATH": str(model),
                    "DIMON_GPU_OVERLORD_CLIENT_DIR": str(client),
                }
            )

            result = subprocess.run(
                [str(ROOT / "services/faster_whisper_cuda/run.sh"), "--check"],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "faster_whisper_cuda: READY")

    def test_cuda_medium_check_allows_standard_venv_interpreter_symlink(self) -> None:
        runtime_root = ROOT / ".runtime"
        runtime_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runtime_root) as temp, tempfile.TemporaryDirectory() as external:
            owned = Path(temp)
            python = owned / "venv/bin/python"
            model = owned / "model"
            client = Path(external)
            python.parent.mkdir(parents=True)
            python.symlink_to("/usr/bin/python3")
            model.mkdir()
            (model / "model.bin").write_bytes(b"model")
            (model / ".dimon-artifact.json").write_text("{}", encoding="utf-8")
            (client / "gpu_overlord_client.py").write_text("", encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "DIMON_PROJECT_PYTHON": str(ROOT / ".venv/bin/python"),
                    "DIMON_FASTER_WHISPER_PYTHON": str(python),
                    "DIMON_FASTER_WHISPER_MODEL_PATH": str(model),
                    "DIMON_GPU_OVERLORD_CLIENT_DIR": str(client),
                }
            )

            result = subprocess.run(
                [str(ROOT / "services/faster_whisper_cuda/run.sh"), "--check"],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
