"""Install and validate the repository-owned Faster-Whisper runtime."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


REQUIRED_MODEL = "medium"
REQUIRED_DEVICE = "cuda"
REQUIRED_COMPUTE_TYPE = "int8_float16"
MODEL_MARKER = ".dimon-artifact.json"


@dataclass(frozen=True)
class RuntimeLayout:
    repository_root: Path
    runtime_root: Path
    venv: Path
    python: Path
    model: Path
    cache_root: Path
    uv_cache: Path
    pip_cache: Path
    huggingface_cache: Path
    huggingface_hub_cache: Path
    transformers_cache: Path
    xdg_cache: Path

    @classmethod
    def for_repository(cls, repository_root: Path) -> "RuntimeLayout":
        root = repository_root.resolve()
        runtime = root / ".runtime"
        venv = runtime / "faster-whisper-medium" / "venv"
        cache = runtime / "cache"
        huggingface = cache / "huggingface"
        return cls(
            repository_root=root,
            runtime_root=runtime,
            venv=venv,
            python=venv / "bin" / "python",
            model=runtime / "models" / "faster-whisper-medium",
            cache_root=cache,
            uv_cache=cache / "uv",
            pip_cache=cache / "pip",
            huggingface_cache=huggingface,
            huggingface_hub_cache=huggingface / "hub",
            transformers_cache=huggingface / "transformers",
            xdg_cache=cache / "xdg",
        )

    @property
    def owned_paths(self) -> tuple[Path, ...]:
        # A stdlib venv intentionally symlinks bin/python to the selected base
        # interpreter.  Validate the owned venv directory itself, not the
        # resolved target of that expected interpreter symlink.
        return (
            self.venv,
            self.model,
            self.cache_root,
            self.uv_cache,
            self.pip_cache,
            self.huggingface_cache,
            self.huggingface_hub_cache,
            self.transformers_cache,
            self.xdg_cache,
        )


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolved_for_containment(path: Path) -> Path:
    """Resolve existing ancestors too, so a cache symlink cannot escape."""
    missing: list[str] = []
    current = path
    while not current.exists() and not current.is_symlink():
        missing.append(current.name)
        current = current.parent
    resolved = current.resolve()
    for name in reversed(missing):
        resolved /= name
    return resolved


def validate_layout(layout: RuntimeLayout) -> None:
    runtime = _resolved_for_containment(layout.runtime_root)
    repository = layout.repository_root.resolve()
    if not runtime.is_relative_to(repository) or runtime == repository:
        raise ValueError("runtime root is outside repository")
    for path in layout.owned_paths:
        resolved = _resolved_for_containment(path)
        if not resolved.is_relative_to(runtime):
            raise ValueError(f"path resolves outside repository runtime: {path.name}")


def build_install_environment(
    layout: RuntimeLayout, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    validate_layout(layout)
    environment = dict(base if base is not None else os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PIP_USER"):
        environment.pop(name, None)
    environment.update(
        {
            "PYTHONNOUSERSITE": "1",
            "UV_CACHE_DIR": str(layout.uv_cache),
            "PIP_CACHE_DIR": str(layout.pip_cache),
            "HF_HOME": str(layout.huggingface_cache),
            "HUGGINGFACE_HUB_CACHE": str(layout.huggingface_hub_cache),
            "TRANSFORMERS_CACHE": str(layout.transformers_cache),
            "XDG_CACHE_HOME": str(layout.xdg_cache),
        }
    )
    return environment


def load_artifact_manifest(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("artifact manifest must be an object")
    return payload


def validate_manifest(manifest: Mapping[str, object]) -> None:
    identity = manifest.get("identity")
    model = manifest.get("model")
    if not isinstance(identity, Mapping) or (
        identity.get("model"),
        identity.get("device"),
        identity.get("compute_type"),
    ) != (REQUIRED_MODEL, REQUIRED_DEVICE, REQUIRED_COMPUTE_TYPE):
        raise ValueError("artifact manifest does not match required worker identity")
    if not isinstance(model, Mapping):
        raise ValueError("artifact manifest model is missing")
    revision = model.get("revision")
    if (
        model.get("repository") != "Systran/faster-whisper-medium"
        or not isinstance(revision, str)
        or len(revision) != 40
        or any(character not in "0123456789abcdef" for character in revision)
    ):
        raise ValueError("artifact manifest model pin is invalid")


def _library_directories(layout: RuntimeLayout) -> tuple[Path, Path]:
    site_packages = next((layout.venv / "lib").glob("python3.*/site-packages"), None)
    if site_packages is None:
        site_packages = layout.venv / "lib" / "python3.12" / "site-packages"
    return (
        site_packages / "nvidia" / "cublas" / "lib",
        site_packages / "nvidia" / "cudnn" / "lib",
    )


def runtime_environment(
    layout: RuntimeLayout, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    environment = build_install_environment(layout, base)
    libraries = os.pathsep.join(str(path) for path in _library_directories(layout))
    existing = environment.get("LD_LIBRARY_PATH", "")
    environment["LD_LIBRARY_PATH"] = libraries + (os.pathsep + existing if existing else "")
    environment["PYTHONPATH"] = str(layout.repository_root)
    return environment


def _run(command: Sequence[str], *, environment: Mapping[str, str]) -> None:
    subprocess.run(list(command), env=dict(environment), check=True)


def install(layout: RuntimeLayout) -> None:
    validate_layout(layout)
    manifest_path = layout.repository_root / "services/faster_whisper_cuda/artifacts.json"
    requirements = layout.repository_root / "services/faster_whisper_cuda/requirements.lock"
    manifest = load_artifact_manifest(manifest_path)
    validate_manifest(manifest)
    for path in (
        layout.venv.parent,
        layout.model,
        layout.uv_cache,
        layout.pip_cache,
        layout.huggingface_hub_cache,
        layout.transformers_cache,
        layout.xdg_cache,
    ):
        path.mkdir(parents=True, exist_ok=True)
    environment = build_install_environment(layout)
    if not layout.python.is_file():
        _run(
            ("uv", "venv", "--python", "/usr/bin/python3.12", str(layout.venv)),
            environment=environment,
        )
    _run(
        (
            "uv",
            "pip",
            "install",
            "--python",
            str(layout.python),
            "--require-hashes",
            "--no-deps",
            "--requirement",
            str(requirements),
        ),
        environment=environment,
    )
    model = manifest["model"]
    assert isinstance(model, Mapping)
    download_environment = runtime_environment(layout, environment)
    _run(
        (
            str(layout.python),
            str(Path(__file__).resolve()),
            "--download-model-worker",
            str(model["repository"]),
            str(model["revision"]),
            str(layout.model),
        ),
        environment=download_environment,
    )
    (layout.model / MODEL_MARKER).write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )


def download_model_worker(repository: str, revision: str, destination: Path) -> None:
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=repository,
        revision=revision,
        local_dir=str(destination),
    )


def check(layout: RuntimeLayout) -> None:
    validate_layout(layout)
    manifest_path = layout.repository_root / "services/faster_whisper_cuda/artifacts.json"
    manifest = load_artifact_manifest(manifest_path)
    validate_manifest(manifest)
    if not layout.python.is_file() or not os.access(layout.python, os.X_OK):
        raise RuntimeError("RUNTIME_MISSING")
    marker = layout.model / MODEL_MARKER
    if not marker.is_file() or not (layout.model / "model.bin").is_file():
        raise RuntimeError("MODEL_MISSING")
    installed = load_artifact_manifest(marker)
    if installed != manifest:
        raise RuntimeError("MODEL_REVISION_MISMATCH")
    probe = (
        "import json, pathlib, ctranslate2, faster_whisper; "
        "print(json.dumps({'ctranslate2': ctranslate2.__file__, "
        "'faster_whisper': faster_whisper.__file__, "
        "'cuda_devices': ctranslate2.get_cuda_device_count()}))"
    )
    result = subprocess.run(
        [str(layout.python), "-c", probe],
        env=runtime_environment(layout),
        check=True,
        text=True,
        capture_output=True,
    )
    status = json.loads(result.stdout)
    for key in ("ctranslate2", "faster_whisper"):
        module_path = Path(status[key]).resolve()
        if not module_path.is_relative_to(layout.venv.resolve()):
            raise RuntimeError("RUNTIME_IMPORT_ESCAPE")
    if int(status["cuda_devices"]) < 1:
        raise RuntimeError("CUDA_UNAVAILABLE")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--install", action="store_true")
    action.add_argument("--check", action="store_true")
    action.add_argument("--download-model-worker", nargs=3, metavar=("REPO", "REV", "DEST"))
    args = parser.parse_args(argv)
    if args.download_model_worker:
        repo, revision, destination = args.download_model_worker
        download_model_worker(repo, revision, Path(destination))
        return 0
    layout = RuntimeLayout.for_repository(repository_root())
    try:
        if args.install:
            install(layout)
        else:
            check(layout)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print("faster_whisper_medium_cuda: READY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
