from __future__ import annotations

import asyncio
import unittest

from apps.dimon_live.gpu_overlord_status import (
    GpuServiceStatus,
    GpuStatusSnapshot,
)
from services.inference_gateway.residency import (
    FourWorkerResidencyGate,
    ProbeEvidence,
)


EXPECTED = {
    "qwen": ("llama", "qwen35-9b-abliterated-q5-4k", 101, 5_852, 6_528),
    "nemotron": ("nemotron", "nemotron-en", 102, 1_196, 1_536),
    "kokoro": ("kokoro", "kokoro-82m", 103, 860, 1_280),
    "faster_whisper": (
        "faster_whisper",
        "faster-whisper-medium-cuda",
        104,
        1_324,
        1_664,
    ),
}


def service(
    name: str,
    *,
    state: str = "ready",
    profile: str | None = None,
    pid: int | None = None,
    active_jobs: int = 0,
    heartbeat: float = 0.2,
    observed: int | None = None,
    budget: int | None = None,
) -> GpuServiceStatus:
    expected_service, expected_profile, expected_pid, expected_observed, expected_budget = EXPECTED[name]
    return GpuServiceStatus(
        service=expected_service,
        state=state,
        active_jobs=active_jobs,
        worker_pid=expected_pid if pid is None else pid,
        profile=expected_profile if profile is None else profile,
        budget_mb=expected_budget if budget is None else budget,
        observed_vram_mb=expected_observed if observed is None else observed,
        heartbeat_age_seconds=heartbeat,
    )


def snapshot(*services: GpuServiceStatus, free_mb: int = 2_213) -> GpuStatusSnapshot:
    return GpuStatusSnapshot(
        total_mb=12_282,
        free_mb=free_mb,
        safety_reserve_mb=512,
        services=services or tuple(service(name) for name in EXPECTED),
    )


def probes(**pid_overrides: int) -> dict[str, ProbeEvidence]:
    return {
        name: ProbeEvidence(
            name=name,
            worker_pid=pid_overrides.get(name, values[2]),
            ok=True,
            reason_code="READY",
        )
        for name, values in EXPECTED.items()
    }


class FakeStatusClient:
    def __init__(self, value: GpuStatusSnapshot) -> None:
        self.value = value

    async def read(self) -> GpuStatusSnapshot:
        return self.value


class ResidencyGateTests(unittest.TestCase):
    def check(self, value: GpuStatusSnapshot, evidence=None):
        gate = FourWorkerResidencyGate(FakeStatusClient(value), max_heartbeat_age_seconds=5)
        return asyncio.run(gate.check(evidence or probes()))

    def test_ready_requires_four_exact_recent_probe_pids(self) -> None:
        result = self.check(snapshot())

        self.assertTrue(result.ready)
        self.assertEqual(result.reason_code, "READY")
        self.assertEqual(result.free_mb, 2_213)
        self.assertEqual({item.name for item in result.workers}, set(EXPECTED))
        self.assertEqual({item.worker_pid for item in result.workers}, {101, 102, 103, 104})

    def test_missing_pid_wrong_profile_stale_over_budget_and_changed_pid_fail_closed(self) -> None:
        cases = (
            (
                snapshot(service("qwen", pid=0), *(service(name) for name in tuple(EXPECTED)[1:])),
                probes(),
                "WORKER_MISSING",
            ),
            (
                snapshot(service("qwen", profile="wrong"), *(service(name) for name in tuple(EXPECTED)[1:])),
                probes(),
                "PROFILE_MISMATCH",
            ),
            (
                snapshot(service("qwen", heartbeat=5.01), *(service(name) for name in tuple(EXPECTED)[1:])),
                probes(),
                "WORKER_STALE",
            ),
            (
                snapshot(service("qwen", observed=6_529), *(service(name) for name in tuple(EXPECTED)[1:])),
                probes(),
                "WORKER_OVER_BUDGET",
            ),
            (snapshot(), probes(qwen=999), "WORKER_PID_CHANGED"),
        )
        for value, evidence, reason in cases:
            with self.subTest(reason=reason):
                result = self.check(value, evidence)
                self.assertFalse(result.ready)
                self.assertEqual(result.reason_code, reason)

    def test_low_reserve_failed_probe_invalid_state_and_nemotron_over_capacity_fail(self) -> None:
        failed = probes()
        failed["kokoro"] = ProbeEvidence("kokoro", 103, False, "KOKORO_UNAVAILABLE")
        cases = (
            (snapshot(free_mb=511), probes(), "GPU_RESERVE_LOW"),
            (snapshot(), failed, "PROBE_FAILED"),
            (
                snapshot(service("qwen", state="idle"), *(service(name) for name in tuple(EXPECTED)[1:])),
                probes(),
                "WORKER_STATE_INVALID",
            ),
            (
                snapshot(
                    service("qwen"),
                    service("nemotron", state="busy", active_jobs=2),
                    service("kokoro"),
                    service("faster_whisper"),
                ),
                probes(),
                "NEMOTRON_OVER_CAPACITY",
            ),
        )
        for value, evidence, reason in cases:
            with self.subTest(reason=reason):
                result = self.check(value, evidence)
                self.assertFalse(result.ready)
                self.assertEqual(result.reason_code, reason)


if __name__ == "__main__":
    unittest.main()
