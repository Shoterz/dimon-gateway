from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from services.gpu_host.model_manifest import ModelManifest


ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "config" / "models.example.yaml"
SCRIPT = ROOT / "scripts" / "check-model-manifest"


def artifact(name: str, port: int) -> dict:
    content = f"fixture-{name}".encode()
    return {
        "source": f"https://huggingface.co/example/{name}",
        "revision": "a" * 40,
        "license": "Apache-2.0",
        "artifact": f"{name}.bin",
        "path_env": f"DIMON_{name.upper()}_MODEL_PATH",
        "expected_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "port": port,
        "download_url": (
            f"https://huggingface.co/example/{name}/resolve/{'a' * 40}/{name}.bin"
        ),
    }


def manifest_data() -> dict:
    return {
        "manifest_version": 1,
        "models": {
            "qwen": artifact("qwen", 8080),
            "nemotron": artifact("nemotron", 8004),
            "kokoro": artifact("kokoro", 3001),
        },
    }


class ModelManifestTests(unittest.TestCase):
    def write(self, data: dict, root: Path) -> Path:
        path = root / "models.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    def test_checked_in_example_has_complete_pinned_path_free_entries(self) -> None:
        manifest = ModelManifest.load(EXAMPLE)

        self.assertEqual(set(manifest.models), {"qwen", "nemotron", "kokoro"})
        self.assertEqual({model.port for model in manifest.models.values()}, {8080, 8004, 3001})
        raw = EXAMPLE.read_text(encoding="utf-8")
        self.assertNotIn("/home/", raw)
        self.assertNotIn("C:\\", raw)
        self.assertNotIn("revision: main", raw)

    def test_missing_fields_mutable_revisions_and_malformed_hashes_are_rejected(self) -> None:
        cases = []
        missing = manifest_data()
        del missing["models"]["qwen"]["license"]
        cases.append(missing)
        mutable = manifest_data()
        mutable["models"]["qwen"]["revision"] = "main"
        cases.append(mutable)
        malformed = manifest_data()
        malformed["models"]["qwen"]["sha256"] = "abc"
        cases.append(malformed)
        literal_path = manifest_data()
        literal_path["models"]["qwen"]["path_env"] = "/models/qwen.gguf"
        cases.append(literal_path)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for index, case in enumerate(cases):
                with self.subTest(index=index), self.assertRaises(ValueError):
                    ModelManifest.load(self.write(case, root))

    def test_duplicate_ports_and_unknown_models_are_rejected(self) -> None:
        duplicate = manifest_data()
        duplicate["models"]["kokoro"]["port"] = 8080
        unknown = manifest_data()
        unknown["models"]["other"] = artifact("other", 9999)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for case in (duplicate, unknown):
                with self.subTest(case=case), self.assertRaises(ValueError):
                    ModelManifest.load(self.write(case, root))

    def test_verification_streams_files_and_returns_only_safe_reason_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = manifest_data()
            manifest = ModelManifest.load(self.write(data, root))
            env: dict[str, str] = {}
            for name, model in manifest.models.items():
                path = root / model.artifact
                path.write_bytes(f"fixture-{name}".encode())
                env[model.path_env] = str(path)

            valid = manifest.verify(env, chunk_bytes=3)
            self.assertTrue(all(result.ok for result in valid))
            self.assertTrue(all(result.reason_code == "READY" for result in valid))
            self.assertTrue(all("/" not in result.artifact for result in valid))

            (root / "qwen.bin").write_bytes(b"wrong")
            wrong_size = manifest.verify(env)
            self.assertEqual(
                next(item for item in wrong_size if item.artifact == "qwen").reason_code,
                "SIZE_MISMATCH",
            )

            (root / "qwen.bin").write_bytes(b"fixture-qwen")
            data["models"]["qwen"]["sha256"] = "0" * 64
            wrong_hash = ModelManifest.load(self.write(data, root)).verify(env)
            self.assertEqual(
                next(item for item in wrong_hash if item.artifact == "qwen").reason_code,
                "CHECKSUM_MISMATCH",
            )

            del env["DIMON_QWEN_MODEL_PATH"]
            missing = manifest.verify(env)
            self.assertEqual(
                next(item for item in missing if item.artifact == "qwen").reason_code,
                "PATH_NOT_CONFIGURED",
            )

    def test_cli_is_machine_readable_and_never_prints_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_path = self.write(manifest_data(), root)
            env = os.environ.copy()
            for name in ("qwen", "nemotron", "kokoro"):
                path = root / f"{name}.bin"
                path.write_bytes(f"fixture-{name}".encode())
                env[f"DIMON_{name.upper()}_MODEL_PATH"] = str(path)
            result = subprocess.run(
                [str(SCRIPT), "--manifest", str(manifest_path), "--json"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "ready")
            self.assertNotIn(temp, result.stdout)


if __name__ == "__main__":
    unittest.main()
