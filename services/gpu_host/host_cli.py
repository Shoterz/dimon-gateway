"""Commands for checking and running the Dimon GPU host."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Mapping, Sequence

from dotenv import load_dotenv

from .capacity import GpuCapacityBroker, NvidiaSmiTelemetry
from .config import HostConfig
from .model_manifest import ModelManifest
from .processes import ManagedProcess, ProcessStateError, SubprocessAdapter


ROOT = Path(__file__).resolve().parents[2]
SERVICE_PORTS = {"qwen": 8080, "nemotron": 8004, "kokoro": 3001, "gateway": 8891}
CAPABILITY_BY_SERVICE = {"qwen": "intent", "nemotron": "transcription", "kokoro": "speech"}


def runtime_directory(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    base = source.get("XDG_RUNTIME_DIR") or f"/tmp/dimon-{os.getuid()}"
    return Path(base) / "dimon-gateway"


def probe_tcp(host: str, port: int, timeout: float = 1) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def gpu_free_mb() -> int:
    return NvidiaSmiTelemetry().free_vram_mb()


def _load_local_environment() -> None:
    load_dotenv(ROOT / ".env.host", override=False)


def _manifest_path() -> Path:
    configured = ROOT / "config" / "models.yaml"
    return configured if configured.is_file() else ROOT / "config" / "models.example.yaml"


def collect_health(env: Mapping[str, str] | None = None) -> dict:
    source = os.environ if env is None else env
    revisions = {
        "qwen": source.get("DIMON_QWEN_REVISION", ""),
        "nemotron": source.get("DIMON_NEMOTRON_REVISION", ""),
        "kokoro": source.get("DIMON_KOKORO_REVISION", ""),
    }
    try:
        manifest = ModelManifest.load(_manifest_path())
        agreement = all(
            revisions[name] == manifest.models[name].revision for name in revisions
        )
    except ValueError:
        agreement = False
    services = {}
    for name, port in SERVICE_PORTS.items():
        ready = probe_tcp("127.0.0.1", port)
        item: dict[str, str | bool] = {
            "ready": ready,
            "reason_code": "READY" if ready else "UNREACHABLE",
        }
        if name in revisions:
            item["revision"] = revisions[name]
        services[name] = item
    try:
        free_mb: int | None = gpu_free_mb()
        gpu_reason = "READY"
    except Exception:
        free_mb = None
        gpu_reason = "TELEMETRY_UNAVAILABLE"
    ready = all(bool(item["ready"]) for item in services.values()) and agreement
    return {
        "status": "ready" if ready else "not_ready",
        "services": services,
        "gpu": {"free_vram_mb": free_mb, "reason_code": gpu_reason},
        "revision_agreement": agreement,
    }


def _check_runtime_inputs(mode: str) -> list[str]:
    failures: list[str] = []
    if sys.version_info[:2] != (3, 12):
        failures.append("PYTHON_VERSION_UNSUPPORTED")
    if shutil.which("uv") is None:
        failures.append("UV_NOT_FOUND")
    try:
        gpu_free_mb()
    except Exception:
        failures.append("GPU_TELEMETRY_UNAVAILABLE")
    try:
        manifest = ModelManifest.load(_manifest_path())
        failures.extend(result.reason_code for result in manifest.verify() if not result.ok)
    except ValueError:
        failures.append("INVALID_MANIFEST")
    if mode == "shared":
        for name in ("qwen", "nemotron", "kokoro"):
            if not probe_tcp("127.0.0.1", SERVICE_PORTS[name]):
                failures.append(f"{name.upper()}_UNREACHABLE")
    else:
        for name in ("qwen", "nemotron", "kokoro"):
            if probe_tcp("127.0.0.1", SERVICE_PORTS[name]):
                failures.append(f"{name.upper()}_PORT_OCCUPIED")
            result = subprocess.run(
                [str(ROOT / "services" / name / "run.sh"), "--check"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode:
                failures.append(f"{name.upper()}_RUNTIME_INVALID")
    if probe_tcp("127.0.0.1", 8891):
        failures.append("GATEWAY_PORT_OCCUPIED")
    return sorted(set(failures))


def _download_models(manifest: ModelManifest, *, accepted: bool, yes: bool) -> int:
    if not accepted:
        print("--accept-licenses is required with --download", file=sys.stderr)
        return 2
    for model in manifest.models.values():
        destination = os.environ.get(model.path_env, "")
        print(
            f"{model.name}: license={model.license} bytes={model.expected_bytes} "
            f"sha256={model.sha256} destination_variable={model.path_env}"
        )
        if not destination:
            print(f"{model.path_env} is required", file=sys.stderr)
            return 2
    if not yes and input("Type DOWNLOAD to continue: ").strip() != "DOWNLOAD":
        print("download cancelled", file=sys.stderr)
        return 2
    for model in manifest.models.values():
        destination = Path(os.environ[model.path_env])
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
            temporary = Path(output.name)
            with urllib.request.urlopen(model.download_url, timeout=60) as response:
                shutil.copyfileobj(response, output, 1024 * 1024)
        digest = hashlib.sha256()
        with temporary.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        if temporary.stat().st_size != model.expected_bytes or digest.hexdigest() != model.sha256:
            temporary.unlink(missing_ok=True)
            print(f"{model.name}: DOWNLOAD_VERIFICATION_FAILED", file=sys.stderr)
            return 1
        temporary.replace(destination)
    return 0


def setup_command(args) -> int:
    _load_local_environment()
    manifest = ModelManifest.load(_manifest_path())
    if args.download:
        return _download_models(manifest, accepted=args.accept_licenses, yes=args.yes)
    failures = _check_runtime_inputs(args.mode)
    payload = {"mode": args.mode, "status": "ready" if not failures else "not_ready", "failures": failures}
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return 0 if not failures else 1


def _managed(name: str, readiness, state_dir: Path, timeout: float) -> ManagedProcess:
    return ManagedProcess(
        name=name,
        command=(str(ROOT / "services" / name / "run.sh"),),
        state_dir=state_dir,
        readiness=readiness,
        adapter=SubprocessAdapter(),
        startup_timeout_seconds=timeout,
    )


def start_command(args) -> int:
    _load_local_environment()
    if args.check:
        failures = _check_runtime_inputs(args.mode)
        print(json.dumps({"mode": args.mode, "status": "ready" if not failures else "not_ready", "failures": failures}, separators=(",", ":"), sort_keys=True))
        return 0 if not failures else 1
    state_dir = runtime_directory()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    managed: list[ManagedProcess] = []
    leases = []
    broker = GpuCapacityBroker(NvidiaSmiTelemetry(), safety_reserve_mb=512)
    wants_raw = args.components in {"raw", "all"}
    wants_gateway = args.components in {"gateway", "all"}
    if args.mode == "shared" and wants_raw:
        unavailable = [name for name in ("qwen", "nemotron", "kokoro") if not probe_tcp("127.0.0.1", SERVICE_PORTS[name])]
        if unavailable:
            print(json.dumps({"status": "not_ready", "reason_code": "SHARED_SERVICE_UNAVAILABLE", "services": unavailable}))
            return 1
    elif args.mode == "standalone" and wants_raw:
        config = HostConfig.load(ROOT / "config" / "gpu-host.example.yaml")
        broker = GpuCapacityBroker(NvidiaSmiTelemetry(), safety_reserve_mb=config.safety_reserve_mb)
        for name in ("qwen", "nemotron", "kokoro"):
            profile = config.profiles[name]
            try:
                leases.append(broker.acquire(name, profile, 0))
            except Exception:
                print(json.dumps({"status": "not_ready", "reason_code": "GPU_CAPACITY_UNAVAILABLE"}))
                return 1
        for name in ("qwen", "nemotron", "kokoro"):
            profile = config.profiles[name]
            process = _managed(
                name,
                lambda port=SERVICE_PORTS[name]: probe_tcp("127.0.0.1", port),
                state_dir,
                profile.startup_timeout_seconds,
            )
            try:
                process.start()
            except ProcessStateError as error:
                print(json.dumps({"status": "not_ready", "reason_code": str(error)}))
                for started in reversed(managed):
                    started.stop()
                return 1
            managed.append(process)
    gateway = None
    if wants_gateway:
        gateway = _managed(
            "inference_gateway",
            lambda: probe_tcp("127.0.0.1", 8891),
            state_dir,
            30,
        )
        try:
            gateway.start()
        except ProcessStateError as error:
            for process in reversed(managed):
                process.stop()
            print(json.dumps({"status": "not_ready", "reason_code": str(error)}))
            return 1
        managed.append(gateway)
    stopping = False

    def stop(_signum=None, _frame=None):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        while not stopping and managed and all(process.ready() for process in managed):
            time.sleep(1)
    finally:
        for process in reversed(managed):
            process.stop()
        for lease in leases:
            broker.release(lease)
    return 0


def stop_command(_args) -> int:
    state_dir = runtime_directory()
    for name in ("inference_gateway", "kokoro", "nemotron", "qwen"):
        try:
            _managed(name, lambda: False, state_dir, 0).stop()
        except (OSError, ProcessStateError):
            pass
    print(json.dumps({"status": "stopped"}, separators=(",", ":")))
    return 0


def health_command(args) -> int:
    _load_local_environment()
    report = collect_health()
    print(json.dumps(report, separators=(",", ":"), sort_keys=True) if args.json else report["status"])
    return 0 if report["status"] == "ready" else 1


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup")
    setup.add_argument("--mode", choices=("shared", "standalone"), required=True)
    setup.add_argument("--check", action="store_true")
    setup.add_argument("--download", action="store_true")
    setup.add_argument("--accept-licenses", action="store_true")
    setup.add_argument("--yes", action="store_true")
    setup.set_defaults(handler=setup_command)
    start = commands.add_parser("start")
    start.add_argument("--mode", choices=("shared", "standalone"), required=True)
    start.add_argument("--components", choices=("raw", "gateway", "all"), default="all")
    start.add_argument("--check", action="store_true")
    start.set_defaults(handler=start_command)
    stop = commands.add_parser("stop")
    stop.set_defaults(handler=stop_command)
    health = commands.add_parser("health")
    health.add_argument("--json", action="store_true")
    health.set_defaults(handler=health_command)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
