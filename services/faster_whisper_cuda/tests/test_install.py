from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services.faster_whisper_cuda.install import (
    REQUIRED_COMPUTE_TYPE,
    REQUIRED_DEVICE,
    REQUIRED_MODEL,
    RuntimeLayout,
    build_install_environment,
    load_artifact_manifest,
    validate_layout,
    validate_manifest,
)


ROOT = Path(__file__).resolve().parents[3]


class RuntimeLayoutTests(unittest.TestCase):
    def test_all_runtime_paths_are_descendants_of_repository_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            layout = RuntimeLayout.for_repository(root)

            validate_layout(layout)

            for path in layout.owned_paths:
                self.assertTrue(path.is_relative_to(layout.runtime_root))

    def test_symlink_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as outside:
            root = Path(temp).resolve()
            runtime = root / ".runtime"
            runtime.mkdir()
            (runtime / "cache").symlink_to(Path(outside).resolve(), target_is_directory=True)
            layout = RuntimeLayout.for_repository(root)

            with self.assertRaisesRegex(ValueError, "outside repository runtime"):
                validate_layout(layout)

    def test_standard_venv_interpreter_symlink_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            layout = RuntimeLayout.for_repository(root)
            layout.python.parent.mkdir(parents=True)
            layout.python.symlink_to("/usr/bin/python3")

            validate_layout(layout)

    def test_install_environment_redirects_every_package_and_model_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            layout = RuntimeLayout.for_repository(Path(temp).resolve())

            environment = build_install_environment(layout, {"PATH": "/usr/bin"})

            expected = {
                "UV_CACHE_DIR": layout.uv_cache,
                "PIP_CACHE_DIR": layout.pip_cache,
                "HF_HOME": layout.huggingface_cache,
                "HUGGINGFACE_HUB_CACHE": layout.huggingface_hub_cache,
                "TRANSFORMERS_CACHE": layout.transformers_cache,
                "XDG_CACHE_HOME": layout.xdg_cache,
            }
            for name, path in expected.items():
                self.assertEqual(environment[name], str(path))
                self.assertTrue(path.is_relative_to(layout.runtime_root))
            self.assertEqual(environment["PYTHONNOUSERSITE"], "1")

    def test_install_never_uses_user_site_or_project_venv(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            layout = RuntimeLayout.for_repository(root)
            environment = build_install_environment(
                layout,
                {
                    "PATH": "/usr/bin",
                    "PYTHONPATH": "/opt/example/lib/python",
                    "VIRTUAL_ENV": str(root / ".venv"),
                    "PIP_USER": "1",
                },
            )

            self.assertNotIn("PYTHONPATH", environment)
            self.assertNotIn("VIRTUAL_ENV", environment)
            self.assertNotIn("PIP_USER", environment)
            self.assertEqual(environment["PYTHONNOUSERSITE"], "1")
            self.assertTrue(layout.python.is_relative_to(layout.runtime_root))
            self.assertNotEqual(layout.venv, root / ".venv")


class ArtifactManifestTests(unittest.TestCase):
    def test_requirements_and_model_revision_are_exactly_pinned(self) -> None:
        manifest = load_artifact_manifest(ROOT / "services/faster_whisper_cuda/artifacts.json")
        validate_manifest(manifest)

        self.assertEqual(manifest["identity"]["model"], REQUIRED_MODEL)
        self.assertEqual(manifest["identity"]["device"], REQUIRED_DEVICE)
        self.assertEqual(manifest["identity"]["compute_type"], REQUIRED_COMPUTE_TYPE)
        self.assertRegex(manifest["model"]["revision"], r"^[0-9a-f]{40}$")

        lock = (ROOT / "services/faster_whisper_cuda/requirements.lock").read_text(
            encoding="utf-8"
        )
        self.assertIn("faster-whisper==1.2.1", lock)
        self.assertIn("ctranslate2==4.8.3", lock)
        self.assertIn("nvidia-cublas-cu12==", lock)
        self.assertIn("nvidia-cudnn-cu12==", lock)
        self.assertNotIn(">=", lock)
        self.assertNotIn("~=", lock)

    def test_check_rejects_wrong_model_device_or_compute_identity(self) -> None:
        valid = {
            "schema_version": 1,
            "identity": {
                "model": REQUIRED_MODEL,
                "device": REQUIRED_DEVICE,
                "compute_type": REQUIRED_COMPUTE_TYPE,
            },
            "model": {
                "repository": "Systran/faster-whisper-medium",
                "revision": "7832330bcea9a8d5fd6d6637c49fe5d256e98277",
            },
        }
        for field, wrong in (
            ("model", "turbo"),
            ("device", "cpu"),
            ("compute_type", "int8"),
        ):
            payload = json.loads(json.dumps(valid))
            payload["identity"][field] = wrong
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "required worker identity"
            ):
                validate_manifest(payload)


if __name__ == "__main__":
    unittest.main()
