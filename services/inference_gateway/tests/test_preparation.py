from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from apps.dimon_live.gpu_overlord_status import GpuServiceStatus, GpuStatusSnapshot
from apps.dimon_live.voice import VoiceLease
from services.inference_gateway.preparation import GatewayPreparation
from services.inference_gateway.residency import (
    FourWorkerResidencyGate,
    ProbeEvidence,
    ResidencySnapshot,
)


PIDS = {"qwen": 101, "nemotron": 102, "kokoro": 103, "faster_whisper": 104}
PROFILES = {
    "qwen": ("llama", "qwen35-9b-abliterated-q5-4k", 6_528, 5_852),
    "nemotron": ("nemotron", "nemotron-en", 1_536, 1_196),
    "kokoro": ("kokoro", "kokoro-82m", 1_280, 860),
    "faster_whisper": (
        "faster_whisper",
        "faster-whisper-medium-cuda",
        1_664,
        1_324,
    ),
}


def gpu_snapshot(
    *,
    nemotron_jobs: int = 0,
    qwen_jobs: int = 0,
    kokoro_jobs: int = 0,
    qwen_pid: int = 101,
) -> GpuStatusSnapshot:
    workers = []
    jobs_by_name = {
        "qwen": qwen_jobs,
        "nemotron": nemotron_jobs,
        "kokoro": kokoro_jobs,
    }
    for name, (service, profile, budget, observed) in PROFILES.items():
        pid = qwen_pid if name == "qwen" else PIDS[name]
        jobs = jobs_by_name.get(name, 0)
        workers.append(
            GpuServiceStatus(
                service=service,
                state="busy" if jobs else "ready",
                active_jobs=jobs,
                worker_pid=pid,
                profile=profile,
                budget_mb=budget,
                observed_vram_mb=observed,
                heartbeat_age_seconds=0.1,
            )
        )
    return GpuStatusSnapshot(12_282, 2_213, 512, tuple(workers))


class StatusClient:
    def __init__(self, value: GpuStatusSnapshot | None = None) -> None:
        self.value = value or gpu_snapshot()
        self.reads = 0

    async def read(self) -> GpuStatusSnapshot:
        self.reads += 1
        return self.value


class SequenceStatusClient:
    def __init__(self, *values: GpuStatusSnapshot) -> None:
        self.values = list(values)
        self.reads = 0

    async def read(self) -> GpuStatusSnapshot:
        value = self.values[min(self.reads, len(self.values) - 1)]
        self.reads += 1
        return value


class Stream:
    def __init__(self, latch=None) -> None:
        self.closed = False
        self.latch = latch

    async def close(self) -> None:
        if self.latch is not None:
            await self.latch.arrive()
        self.closed = True


class Recognizer:
    def __init__(self, latch=None) -> None:
        self.opens = 0
        self.stream = None
        self.latch = latch
        self.fail = False

    async def open_stream(self, _partial):
        self.opens += 1
        if self.fail:
            raise RuntimeError("nemotron unavailable")
        self.stream = Stream(self.latch)
        return self.stream


class Intent:
    def __init__(self, latch=None) -> None:
        self.calls = 0
        self.latch = latch

    async def extract(self, _text):
        self.calls += 1
        if self.latch is not None:
            await self.latch.arrive()


class Probe:
    def __init__(self, name: str, *, latch=None) -> None:
        self.name = name
        self.calls = 0
        self.ok = True
        self.latch = latch

    async def __call__(self) -> ProbeEvidence:
        self.calls += 1
        if self.latch is not None:
            await self.latch.arrive()
        return ProbeEvidence(
            self.name,
            PIDS[self.name] if self.ok else 0,
            self.ok,
            "READY" if self.ok else f"{self.name.upper()}_UNAVAILABLE",
        )


class Latch:
    def __init__(self, count: int) -> None:
        self.count = count
        self.arrivals = 0
        self.ready = asyncio.Event()

    async def arrive(self) -> None:
        self.arrivals += 1
        if self.arrivals >= self.count:
            self.ready.set()
        await asyncio.wait_for(self.ready.wait(), timeout=1)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 10, 15, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.value


def coordinator(
    *,
    status_client: StatusClient | None = None,
    recognizer: Recognizer | None = None,
    intent: Intent | None = None,
    speech=None,
    qwen_probe: Probe | None = None,
    faster_probe: Probe | None = None,
    lease: VoiceLease | None = None,
    clock: Clock | None = None,
    health_interval_seconds: float = 45,
    worker_resetter=None,
    residency_gate=None,
    startup_retry_seconds: float = 0.001,
    startup_timeout_seconds: float = 0.05,
) -> GatewayPreparation:
    client = status_client or StatusClient()
    selected_speech = speech
    if selected_speech is None:
        async def selected_speech(_text):
            return SimpleNamespace(data=b"RIFF\x00\x00\x00\x00WAVE")
    return GatewayPreparation(
        recognizer=recognizer or Recognizer(),
        intent_upstream=intent or Intent(),
        speech_synthesizer=selected_speech,
        residency_gate=residency_gate or FourWorkerResidencyGate(client),
        qwen_probe=qwen_probe or Probe("qwen"),
        faster_whisper_probe=faster_probe or Probe("faster_whisper"),
        transcription_lease=lease or VoiceLease(1),
        health_interval_seconds=health_interval_seconds,
        utc_now=clock.now if clock else None,
        worker_resetter=worker_resetter,
        startup_retry_seconds=startup_retry_seconds,
        startup_timeout_seconds=startup_timeout_seconds,
    )


async def start_and_wait(preparation: GatewayPreparation):
    await preparation.start()
    await preparation.task
    return await preparation.status()


class PreparationTests(unittest.TestCase):
    def test_reset_finishes_before_any_worker_probe_and_ready_uses_new_pids(self) -> None:
        async def scenario():
            reset_finished = False
            reset_calls = 0

            async def reset_workers():
                nonlocal reset_finished, reset_calls
                reset_calls += 1
                reset_finished = True
                return frozenset({1, 2, 3, 4})

            class GuardedRecognizer(Recognizer):
                async def open_stream(self, partial):
                    self.assert_reset_finished()
                    return await super().open_stream(partial)

                @staticmethod
                def assert_reset_finished():
                    if not reset_finished:
                        raise AssertionError("Nemotron probed before GPU reset finished")

            class GuardedIntent(Intent):
                async def extract(self, text):
                    if not reset_finished:
                        raise AssertionError("Qwen probed before GPU reset finished")
                    return await super().extract(text)

            async def guarded_speech(_text):
                if not reset_finished:
                    raise AssertionError("Kokoro probed before GPU reset finished")
                return SimpleNamespace(data=b"RIFF\x00\x00\x00\x00WAVE")

            class GuardedProbe(Probe):
                async def __call__(self):
                    if not reset_finished:
                        raise AssertionError("Faster-Whisper probed before GPU reset finished")
                    return await super().__call__()

            preparation = coordinator(
                recognizer=GuardedRecognizer(),
                intent=GuardedIntent(),
                speech=guarded_speech,
                faster_probe=GuardedProbe("faster_whisper"),
                worker_resetter=reset_workers,
            )
            await asyncio.gather(preparation.start(), preparation.start())
            await preparation.task
            result = await preparation.status()
            await preparation.stop()
            return result, reset_calls

        result, reset_calls = asyncio.run(scenario())

        self.assertEqual(reset_calls, 1)
        self.assertEqual(result["state"], "READY")
        self.assertEqual(
            {worker["worker_pid"] for worker in result["workers"]},
            {101, 102, 103, 104},
        )

    def test_post_reset_probe_retries_until_kokoro_frontend_is_ready(self) -> None:
        async def scenario():
            calls = 0

            async def reset_workers():
                return frozenset({1, 2, 3, 4})

            async def delayed_speech(_text):
                nonlocal calls
                calls += 1
                if calls < 3:
                    raise ConnectionError("Kokoro frontend is still starting")
                return SimpleNamespace(data=b"RIFF\x00\x00\x00\x00WAVE")

            preparation = coordinator(
                speech=delayed_speech,
                worker_resetter=reset_workers,
            )
            result = await start_and_wait(preparation)
            await preparation.stop()
            return result, calls

        result, calls = asyncio.run(scenario())

        self.assertEqual(result["state"], "READY")
        self.assertEqual(calls, 3)

    def test_post_reset_residency_waits_for_worker_heartbeat(self) -> None:
        async def scenario():
            status = StatusClient()

            class DelayedResidencyGate:
                def __init__(self) -> None:
                    self.status_client = status
                    self.calls = 0
                    self.real = FourWorkerResidencyGate(status)

                async def check(self, evidence):
                    self.calls += 1
                    if self.calls == 1:
                        return ResidencySnapshot(
                            False,
                            "WORKER_MISSING",
                            12_282,
                            2_213,
                            512,
                            (),
                        )
                    return await self.real.check(evidence)

            gate = DelayedResidencyGate()

            async def reset_workers():
                return frozenset({1, 2, 3, 4})

            preparation = coordinator(
                status_client=status,
                residency_gate=gate,
                worker_resetter=reset_workers,
            )
            result = await start_and_wait(preparation)
            await preparation.stop()
            return result, gate.calls

        result, calls = asyncio.run(scenario())

        self.assertEqual(result["state"], "READY")
        self.assertEqual(calls, 2)

    def test_post_reset_warms_larger_workers_before_recognizers(self) -> None:
        async def scenario():
            events = []

            async def reset_workers():
                return frozenset({1, 2, 3, 4})

            preparation = coordinator(
                worker_resetter=reset_workers,
            )

            async def probe(name):
                events.append(name)
                return ProbeEvidence(name, PIDS[name], True, "READY")

            preparation._probe_qwen = lambda: probe("qwen")
            preparation._probe_kokoro = lambda: probe("kokoro")
            preparation._probe_nemotron = lambda: probe("nemotron")
            preparation.faster_whisper_probe = lambda: probe("faster_whisper")
            status = await start_and_wait(preparation)
            await preparation.stop()
            return status, events

        status, events = asyncio.run(scenario())
        self.assertEqual(status["state"], "READY")
        self.assertEqual(events, ["qwen", "kokoro", "nemotron", "faster_whisper"])
        self.assertEqual(
            [stage["name"] for stage in status["stages"]],
            ["capacity", "nemotron", "qwen", "kokoro", "faster_whisper"],
        )
        self.assertTrue(all(stage["state"] == "READY" for stage in status["stages"]))
        self.assertEqual(len(status["workers"]), 4)
        self.assertEqual(status["gpu"]["free_mb"], 2_213)

    def test_starting_a_new_run_cancels_the_previous_health_monitor(self) -> None:
        async def scenario():
            entered = asyncio.Event()
            release = asyncio.Event()

            class BlockingSecondProbe(Probe):
                async def __call__(self):
                    self.calls += 1
                    if self.calls == 2:
                        entered.set()
                        await release.wait()
                    return ProbeEvidence(self.name, PIDS[self.name], True, "READY")

            qwen = BlockingSecondProbe("qwen")
            preparation = coordinator(
                qwen_probe=qwen,
                health_interval_seconds=0.01,
            )
            await start_and_wait(preparation)
            await asyncio.sleep(0)
            await preparation.start()
            await entered.wait()
            await asyncio.sleep(0.02)
            during = await preparation.status()
            release.set()
            await preparation.task
            after = await preparation.status()
            await preparation.stop()
            return during, after

        during, after = asyncio.run(scenario())
        self.assertEqual(during["state"], "PREPARING")
        self.assertEqual(after["state"], "READY")

    def test_legitimately_busy_nemotron_counts_as_live_probe_without_competing_stream(self) -> None:
        recognizer = Recognizer()
        lease = VoiceLease(1)
        asyncio.run(lease.acquire())
        status = asyncio.run(
            start_and_wait(
                coordinator(
                    status_client=StatusClient(gpu_snapshot(nemotron_jobs=1)),
                    recognizer=recognizer,
                    lease=lease,
                )
            )
        )

        self.assertEqual(status["state"], "READY")
        self.assertEqual(recognizer.opens, 0)
        nemotron = next(item for item in status["workers"] if item["name"] == "nemotron")
        self.assertEqual(nemotron["state"], "busy")
        self.assertEqual(nemotron["active_jobs"], 1)

    def test_unrelated_transcription_lease_does_not_block_nemotron_warmup(self) -> None:
        async def scenario():
            recognizer = Recognizer()
            lease = VoiceLease(1)
            self.assertTrue(await lease.acquire())
            preparation = coordinator(recognizer=recognizer, lease=lease)
            try:
                status = await start_and_wait(preparation)
            finally:
                await preparation.stop()
                lease.release()
            return status, recognizer

        status, recognizer = asyncio.run(scenario())

        self.assertEqual(status["state"], "READY")
        self.assertEqual(recognizer.opens, 1)
        self.assertTrue(recognizer.stream.closed)

    def test_nemotron_probe_race_accepts_the_real_session_that_won_capacity(self) -> None:
        idle_workers = tuple(
            worker
            for worker in gpu_snapshot().services
            if worker.service != "nemotron"
        ) + (
            GpuServiceStatus(
                service="nemotron",
                state="idle",
                active_jobs=0,
                worker_pid=None,
                profile=None,
                budget_mb=0,
                observed_vram_mb=0,
                heartbeat_age_seconds=0.1,
            ),
        )
        status_client = SequenceStatusClient(
            GpuStatusSnapshot(12_282, 3_432, 512, idle_workers),
            gpu_snapshot(nemotron_jobs=1),
        )
        recognizer = Recognizer()
        recognizer.fail = True
        preparation = coordinator(
            status_client=status_client,
            recognizer=recognizer,
        )

        evidence = asyncio.run(preparation._probe_nemotron())

        self.assertEqual(
            evidence,
            ProbeEvidence("nemotron", PIDS["nemotron"], True, "READY"),
        )

    def test_busy_qwen_and_kokoro_are_verified_without_competing_work(self) -> None:
        class UnexpectedIntent:
            async def extract(self, _text):
                raise AssertionError("busy Qwen must not receive a warm-up generation")

        async def unexpected_speech(_text):
            raise AssertionError("busy Kokoro must not receive a warm-up synthesis")

        status = asyncio.run(
            start_and_wait(
                coordinator(
                    status_client=StatusClient(
                        gpu_snapshot(qwen_jobs=1, kokoro_jobs=1)
                    ),
                    intent=UnexpectedIntent(),
                    speech=unexpected_speech,
                )
            )
        )

        self.assertEqual(status["state"], "READY")
        workers = {worker["name"]: worker for worker in status["workers"]}
        self.assertEqual(workers["qwen"]["state"], "busy")
        self.assertEqual(workers["kokoro"]["state"], "busy")

    def test_over_capacity_nemotron_and_pid_change_degrade_final_gate(self) -> None:
        over_capacity = asyncio.run(
            start_and_wait(
                coordinator(status_client=StatusClient(gpu_snapshot(nemotron_jobs=2)))
            )
        )
        changed_pid = asyncio.run(
            start_and_wait(
                coordinator(status_client=StatusClient(gpu_snapshot(qwen_pid=999)))
            )
        )

        self.assertEqual(over_capacity["state"], "DEGRADED")
        self.assertEqual(over_capacity["reason_code"], "NEMOTRON_OVER_CAPACITY")
        self.assertEqual(changed_pid["state"], "DEGRADED")
        self.assertEqual(changed_pid["reason_code"], "WORKER_PID_CHANGED")

    def test_monitor_degrades_and_recovers_all_worker_probes(self) -> None:
        async def scenario():
            faster = Probe("faster_whisper")
            preparation = coordinator(
                faster_probe=faster,
                health_interval_seconds=0.01,
            )
            await start_and_wait(preparation)
            faster.ok = False
            await asyncio.sleep(0.025)
            degraded = await preparation.status()
            faster.ok = True
            await asyncio.sleep(0.025)
            recovered = await preparation.status()
            await preparation.stop()
            return degraded, recovered, faster.calls

        degraded, recovered, calls = asyncio.run(scenario())
        self.assertEqual(degraded["state"], "DEGRADED")
        self.assertEqual(
            next(stage for stage in degraded["stages"] if stage["name"] == "faster_whisper")["state"],
            "FAILED",
        )
        self.assertEqual(recovered["state"], "READY")
        self.assertGreaterEqual(calls, 3)

    def test_ready_window_expiry_resets_overall_and_every_stage_to_idle(self) -> None:
        async def scenario():
            clock = Clock()
            preparation = coordinator(clock=clock)
            ready = await start_and_wait(preparation)
            clock.value += timedelta(seconds=301)
            expired = await preparation.status()
            await preparation.stop()
            return ready, expired

        ready, expired = asyncio.run(scenario())
        self.assertEqual(ready["state"], "READY")
        self.assertEqual(expired["state"], "IDLE")
        self.assertTrue(all(stage["state"] == "IDLE" for stage in expired["stages"]))
        self.assertEqual(expired["workers"], [])
        self.assertIsNone(expired["gpu"])


if __name__ == "__main__":
    unittest.main()
