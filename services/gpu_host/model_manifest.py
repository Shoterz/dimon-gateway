"""Pinned model artifact manifest parsing and verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import yaml


MODEL_NAMES = frozenset({"qwen", "nemotron", "kokoro"})
REQUIRED_FIELDS = frozenset(
    {
        "source",
        "revision",
        "license",
        "artifact",
        "path_env",
        "expected_bytes",
        "sha256",
        "port",
        "download_url",
    }
)
REVISION_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
ENV_RE = re.compile(r"DIMON_[A-Z0-9_]+_PATH")


@dataclass(frozen=True)
class ModelArtifact:
    name: str
    source: str
    revision: str
    license: str
    artifact: str
    path_env: str
    expected_bytes: int
    sha256: str
    port: int
    download_url: str


@dataclass(frozen=True)
class VerificationResult:
    artifact: str
    ok: bool
    reason_code: str

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "artifact": self.artifact,
            "ok": self.ok,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class ModelManifest:
    models: dict[str, ModelArtifact]

    @classmethod
    def load(cls, path: str | Path) -> "ModelManifest":
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise ValueError("invalid model manifest") from error
        if not isinstance(raw, dict) or raw.get("manifest_version") != 1:
            raise ValueError("unsupported model manifest version")
        models_raw = raw.get("models")
        if not isinstance(models_raw, dict) or set(models_raw) != MODEL_NAMES:
            raise ValueError("manifest must define exactly qwen, nemotron, and kokoro")

        models: dict[str, ModelArtifact] = {}
        ports: set[int] = set()
        for name in models_raw:
            entry = models_raw[name]
            if not isinstance(entry, dict) or set(entry) != REQUIRED_FIELDS:
                raise ValueError(f"invalid fields for {name}")
            cls._validate_entry(name, entry)
            port = entry["port"]
            if port in ports:
                raise ValueError("model ports must be unique")
            ports.add(port)
            models[name] = ModelArtifact(name=name, **entry)
        return cls(models=models)

    @staticmethod
    def _validate_entry(name: str, entry: Mapping[str, object]) -> None:
        string_fields = ("source", "license", "artifact", "download_url")
        if any(not isinstance(entry[field], str) or not entry[field] for field in string_fields):
            raise ValueError(f"invalid text field for {name}")
        if not str(entry["source"]).startswith("https://"):
            raise ValueError(f"source must use HTTPS for {name}")
        if not str(entry["download_url"]).startswith("https://"):
            raise ValueError(f"download URL must use HTTPS for {name}")
        revision = entry["revision"]
        if not isinstance(revision, str) or REVISION_RE.fullmatch(revision) is None:
            raise ValueError(f"revision must be an immutable commit for {name}")
        if revision not in str(entry["download_url"]):
            raise ValueError(f"download URL must contain the pinned revision for {name}")
        if not isinstance(entry["sha256"], str) or SHA256_RE.fullmatch(entry["sha256"]) is None:
            raise ValueError(f"invalid SHA-256 for {name}")
        if not isinstance(entry["path_env"], str) or ENV_RE.fullmatch(entry["path_env"]) is None:
            raise ValueError(f"path_env must name a DIMON path variable for {name}")
        artifact = Path(str(entry["artifact"]))
        if artifact.name != str(entry["artifact"]) or artifact.name in {"", ".", ".."}:
            raise ValueError(f"artifact must be a filename for {name}")
        expected_bytes = entry["expected_bytes"]
        if isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int) or expected_bytes <= 0:
            raise ValueError(f"expected_bytes must be positive for {name}")
        port = entry["port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f"invalid port for {name}")

    def verify(
        self,
        env: Mapping[str, str] | None = None,
        *,
        chunk_bytes: int = 1024 * 1024,
    ) -> list[VerificationResult]:
        if chunk_bytes <= 0:
            raise ValueError("chunk_bytes must be positive")
        configured = os.environ if env is None else env
        results: list[VerificationResult] = []
        for name, model in self.models.items():
            configured_path = configured.get(model.path_env)
            if not configured_path:
                results.append(VerificationResult(name, False, "PATH_NOT_CONFIGURED"))
                continue
            path = Path(configured_path)
            try:
                stat = path.stat()
            except OSError:
                results.append(VerificationResult(name, False, "ARTIFACT_NOT_FOUND"))
                continue
            if not path.is_file():
                results.append(VerificationResult(name, False, "ARTIFACT_NOT_FOUND"))
                continue
            if stat.st_size != model.expected_bytes:
                results.append(VerificationResult(name, False, "SIZE_MISMATCH"))
                continue
            digest = hashlib.sha256()
            try:
                with path.open("rb") as stream:
                    while chunk := stream.read(chunk_bytes):
                        digest.update(chunk)
            except OSError:
                results.append(VerificationResult(name, False, "ARTIFACT_UNREADABLE"))
                continue
            if digest.hexdigest() != model.sha256:
                results.append(VerificationResult(name, False, "CHECKSUM_MISMATCH"))
                continue
            results.append(VerificationResult(name, True, "READY"))
        return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify Dimon model artifacts")
    parser.add_argument("--manifest", default="config/models.yaml")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        results = ModelManifest.load(args.manifest).verify()
        ready = all(item.ok for item in results)
        payload = {
            "status": "ready" if ready else "not_ready",
            "artifacts": [item.as_dict() for item in results],
        }
    except ValueError:
        ready = False
        payload = {"status": "invalid_manifest", "reason_code": "INVALID_MANIFEST"}
    if args.json:
        print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    else:
        print(payload["status"])
        for result in payload.get("artifacts", []):
            print(f"{result['artifact']}: {result['reason_code']}")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
