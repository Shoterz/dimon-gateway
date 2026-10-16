#!/usr/bin/env python3
"""Measure only verified resident worker PIDs; never record inference content."""

from __future__ import annotations

import argparse
import json
import math
import os
import socket
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

import httpx


REQUIRED_WORKERS = {
    "qwen": {
        "service": "llama",
        "profile": "qwen35-9b-abliterated-q5-4k",
        "model": "qwen35-9b-abliterated-q5",
        "device": "cuda",
        "compute_type": "Q5_K_M",
    },
    "nemotron": {
        "service": "nemotron",
        "profile": "nemotron-en",
        "model": "nemotron-en",
        "device": "cuda",
        "compute_type": "fp16",
    },
    "kokoro": {
        "service": "kokoro",
        "profile": "kokoro-82m",
        "model": "kokoro-82m",
        "device": "cuda",
        "compute_type": "fp32",
    },
    "faster_whisper": {
        "service": "faster_whisper",
        "profile": "faster-whisper-medium-cuda",
        "model": "medium",
        "device": "cuda",
        "compute_type": "int8_float16",
    },
}


@dataclass(frozen=True)
class WorkerEvidence:
    name: str
    service: str
    worker_pid: int
    profile: str
    model: str
    device: str
    compute_type: str


def validate_evidence(workers: Sequence[WorkerEvidence]) -> None:
    if {worker.name for worker in workers} != set(REQUIRED_WORKERS):
        raise ValueError("four required workers are not present")
    pids = [worker.worker_pid for worker in workers]
    if any(not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 for pid in pids):
        raise ValueError("worker PID is invalid")
    if len(set(pids)) != len(pids):
        raise ValueError("each service must have a distinct worker PID")
    for worker in workers:
        expected = REQUIRED_WORKERS[worker.name]
        actual = {
            "service": worker.service,
            "profile": worker.profile,
            "model": worker.model,
            "device": worker.device,
            "compute_type": worker.compute_type,
        }
        if actual != expected:
            raise ValueError(f"{worker.name} identity mismatch")


def parse_compute_apps(output: str, expected_pids: set[int]) -> dict[int, int]:
    samples: dict[int, int] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 2:
            raise ValueError("invalid nvidia-smi compute sample")
        try:
            pid = int(fields[0])
            used_mb = int(fields[1])
        except ValueError as error:
            raise ValueError("invalid nvidia-smi compute sample") from error
        if pid not in expected_pids:
            continue
        if pid in samples:
            raise ValueError("duplicate worker PID in GPU sample")
        if used_mb < 0:
            raise ValueError("negative worker memory sample")
        samples[pid] = used_mb
    missing = expected_pids - samples.keys()
    if missing:
        raise ValueError("missing worker PID from GPU sample")
    return samples


def calculate_budget_mb(loaded_idle_mb: int, inference_peak_mb: int) -> int:
    if loaded_idle_mb < 0 or inference_peak_mb < 0:
        raise ValueError("VRAM samples must be non-negative")
    peak = max(loaded_idle_mb, inference_peak_mb)
    headroom = max(256, math.ceil(peak * 0.10))
    return math.ceil((peak + headroom) / 128) * 128


def validate_reserve(*, total_mb: int, free_mb: int, required_mb: int) -> None:
    if total_mb <= 0 or free_mb < 0 or free_mb > total_mb or required_mb <= 0:
        raise ValueError("invalid GPU memory summary")
    if free_mb < required_mb:
        raise ValueError("measured VRAM reserve is below the required minimum")


def build_report(
    workers: Sequence[WorkerEvidence],
    samples: Mapping[str, Mapping[str, int]],
    *,
    gpu_total_mb: int,
    final_free_mb: int,
    safety_reserve_mb: int,
    measured_at: str,
) -> dict[str, object]:
    validate_evidence(workers)
    validate_reserve(
        total_mb=gpu_total_mb,
        free_mb=final_free_mb,
        required_mb=safety_reserve_mb,
    )
    results: list[dict[str, object]] = []
    for worker in workers:
        sample = samples.get(worker.name)
        if not isinstance(sample, Mapping):
            raise ValueError(f"missing samples for {worker.name}")
        loaded = sample.get("loaded_idle_mb")
        peak = sample.get("inference_peak_mb")
        if not isinstance(loaded, int) or not isinstance(peak, int):
            raise ValueError(f"invalid samples for {worker.name}")
        results.append(
            {
                **asdict(worker),
                "loaded_idle_mb": loaded,
                "inference_peak_mb": peak,
                "calculated_budget_mb": calculate_budget_mb(loaded, peak),
                "probe_ok": True,
            }
        )
    return {
        "schema_version": 1,
        "measured_at": measured_at,
        "gpu": {
            "total_mb": gpu_total_mb,
            "final_free_mb": final_free_mb,
            "safety_reserve_mb": safety_reserve_mb,
        },
        "workers": results,
    }


def read_overlord_status(socket_path: Path) -> dict[str, object]:
    request = b'{"op":"status"}\n'
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        connection.connect(str(socket_path))
        connection.sendall(request)
        buffer = bytearray()
        while not buffer.endswith(b"\n"):
            chunk = connection.recv(65_536 - len(buffer))
            if not chunk or len(buffer) + len(chunk) >= 65_536:
                raise RuntimeError("GPU_STATUS_INVALID")
            buffer.extend(chunk)
    payload = json.loads(buffer)
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError("GPU_STATUS_INVALID")
    return payload


def evidence_from_status(
    response: Mapping[str, object], fwm_health: Mapping[str, object]
) -> tuple[WorkerEvidence, ...]:
    status = response.get("status")
    if not isinstance(status, Mapping) or not isinstance(status.get("services"), list):
        raise ValueError("GPU status is missing services")
    services = {
        item.get("service"): item
        for item in status["services"]
        if isinstance(item, Mapping) and isinstance(item.get("service"), str)
    }
    workers: list[WorkerEvidence] = []
    for name, expected in REQUIRED_WORKERS.items():
        item = services.get(expected["service"])
        if not isinstance(item, Mapping):
            raise ValueError(f"missing resident service {expected['service']}")
        pid = item.get("worker_pid")
        profile = item.get("profile")
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or item.get("state") not in {"ready", "busy"}
            or profile != expected["profile"]
        ):
            raise ValueError(f"service {expected['service']} is not resident")
        model = str(expected["model"])
        device = str(expected["device"])
        compute_type = str(expected["compute_type"])
        if name == "faster_whisper":
            if (
                fwm_health.get("worker_pid") != pid
                or fwm_health.get("ready") is not True
                or fwm_health.get("loaded") is not True
            ):
                raise ValueError("faster_whisper health PID mismatch")
            model = str(fwm_health.get("model"))
            device = str(fwm_health.get("device"))
            compute_type = str(fwm_health.get("compute_type"))
        workers.append(
            WorkerEvidence(
                name=name,
                service=str(expected["service"]),
                worker_pid=pid,
                profile=str(profile),
                model=model,
                device=device,
                compute_type=compute_type,
            )
        )
    result = tuple(workers)
    validate_evidence(result)
    return result


def nvidia_compute_apps() -> str:
    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-compute-apps=pid,used_memory",
            "--format=csv,noheader,nounits",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout


def nvidia_gpu_summary() -> tuple[int, int]:
    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=memory.total,memory.free",
            "--format=csv,noheader,nounits",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    fields = [field.strip() for field in result.stdout.strip().split(",")]
    if len(fields) != 2:
        raise ValueError("invalid nvidia-smi GPU summary")
    return int(fields[0]), int(fields[1])


def sample_live_workers(
    workers: Sequence[WorkerEvidence], *, duration_seconds: float, interval_seconds: float
) -> dict[str, dict[str, int]]:
    if duration_seconds <= 0 or not 0 < interval_seconds <= duration_seconds:
        raise ValueError("invalid sampling interval")
    by_pid = {worker.worker_pid: worker.name for worker in workers}
    initial = parse_compute_apps(nvidia_compute_apps(), set(by_pid))
    peaks = dict(initial)
    deadline = time.monotonic() + duration_seconds
    while time.monotonic() < deadline:
        current = parse_compute_apps(nvidia_compute_apps(), set(by_pid))
        for pid, value in current.items():
            peaks[pid] = max(peaks[pid], value)
        time.sleep(interval_seconds)
    return {
        name: {
            "loaded_idle_mb": initial[pid],
            "inference_peak_mb": peaks[pid],
        }
        for pid, name in by_pid.items()
    }


def default_overlord_socket() -> Path:
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    return runtime / "gpu-overlord.sock"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", type=Path, default=default_overlord_socket())
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        health_response = httpx.get("http://127.0.0.1:8007/health", timeout=3)
        health_response.raise_for_status()
        health = health_response.json()
        if not isinstance(health, dict):
            raise ValueError("invalid Faster-Whisper health")
        response = read_overlord_status(args.socket)
        workers = evidence_from_status(response, health)
        samples = sample_live_workers(
            workers,
            duration_seconds=args.duration,
            interval_seconds=args.interval,
        )
        total, free = nvidia_gpu_summary()
        status = response["status"]
        assert isinstance(status, Mapping)
        gpu = status["gpu"]
        assert isinstance(gpu, Mapping)
        reserve = int(gpu["safety_reserve_mb"])
        report = build_report(
            workers,
            samples,
            gpu_total_mb=total,
            final_free_mb=free,
            safety_reserve_mb=reserve,
            measured_at=datetime.now(timezone.utc).astimezone().isoformat(),
        )
    except Exception as error:
        print(str(error), file=os.sys.stderr)
        return 1
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(encoded, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
