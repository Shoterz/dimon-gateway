import asyncio
import json
import tempfile
import threading
import time
import unittest
from contextlib import chdir
from datetime import datetime
from pathlib import Path

from apps.dimon_live.demo_preparation import DemoPreparationCoordinator
from apps.dimon_live.gpu_overlord_status import GpuServiceStatus, GpuStatusSnapshot
from apps.dimon_live.model_adapter import ExtractedIntent


VALID_WAV = b"RIFF" + (4).to_bytes(4, "little") + b"WAVE"


def service(
    name: str,
    *,
    state: str = "ready",
    active_jobs: int = 0,
    worker_present: bool = True,
) -> GpuServiceStatus:
    profiles = {
        "nemotron": "nemotron-en",
        "llama": "qwen35-9b-abliterated-q5-4k",
        "kokoro": "kokoro-82m",
        "faster_whisper": "faster-whisper-medium-cuda",
    }
    return GpuServiceStatus(
        service=name,
        state=state,
        active_jobs=active_jobs,
        worker_present=worker_present,
        budget_mb=1_024,
        observed_vram_mb=900,
        profile=profiles.get(name),
        heartbeat_age_seconds=0.1,
    )


def gpu_snapshot(*services: GpuServiceStatus) -> GpuStatusSnapshot:
    if not any(item.service == "faster_whisper" for item in services):
        services = (*services, service("faster_whisper"))
    return GpuStatusSnapshot(
        total_mb=12_288,
        free_mb=4_096,
        safety_reserve_mb=512,
        services=tuple(services),
    )


class FakeStatusClient:
    def __init__(self, events, snapshots=None, gate=None):
        self.events = events
        self.snapshots = list(
            snapshots
            or [
                gpu_snapshot(
                    service("whisper", state="idle", worker_present=True),
                    service("nemotron"),
                    service("llama"),
                    service("kokoro"),
                )
            ]
        )
        self.gate = gate

    async def read(self):
        self.events.append("status")
        if self.gate is not None:
            await self.gate.wait()
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]


class FakeLifecycle:
    def __init__(self, events):
        self.events = events

    async def acquire(self):
        self.events.append("lifecycle.acquire")
        return FakeLifecycleLease(self.events)


class FakeLifecycleLease:
    def __init__(self, events):
        self.events = events
        self.released = False

    async def release(self, *, disconnected=False):
        if self.released:
            return
        self.released = True
        self.events.append("lifecycle.release")


class FakeStream:
    def __init__(self, events):
        self.events = events

    async def close(self):
        self.events.append("stream.close")


class FakeRecognizer:
    def __init__(self, events):
        self.events = events

    async def open_stream(self, on_partial):
        self.events.append("recognizer.open")
        return FakeStream(self.events)


class FakeFasterWhisper:
    def __init__(self, events):
        self.events = events

    async def transcribe(self, audio, on_partial):
        self.events.append("faster_whisper.transcribe")
        return ""


class FakeExtractor:
    def __init__(self, events):
        self.events = events

    def extract(self, text):
        self.events.append("extractor.extract")
        return ExtractedIntent({}, "{}", 1.0)


class FakeSynthesizer:
    def __init__(self, events):
        self.events = events

    async def __call__(self, text):
        self.events.append("synthesizer")
        return VALID_WAV


def coordinator(events, *, snapshots=None, gate=None):
    return DemoPreparationCoordinator(
        status_client=FakeStatusClient(events, snapshots, gate),
        extractor=FakeExtractor(events),
        recognizer=FakeRecognizer(events),
        lifecycle=FakeLifecycle(events),
        synthesizer=FakeSynthesizer(events),
        faster_whisper_recognizer=FakeFasterWhisper(events),
    )


class DemoPreparationCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_qwen_and_kokoro_probes_overlap_before_ready(self):
        events = []
        qwen_started = threading.Event()
        qwen_gate = threading.Event()
        kokoro_started = asyncio.Event()
        kokoro_gate = asyncio.Event()

        class GatedExtractor(FakeExtractor):
            def extract(self, text):
                qwen_started.set()
                if not qwen_gate.wait(timeout=2):
                    raise TimeoutError("test Qwen gate timed out")
                return super().extract(text)

        class GatedSynthesizer(FakeSynthesizer):
            async def __call__(self, text):
                self.events.append("synthesizer.started")
                kokoro_started.set()
                await kokoro_gate.wait()
                self.events.append("synthesizer.finished")
                return VALID_WAV

        preparer = DemoPreparationCoordinator(
            status_client=FakeStatusClient(events),
            extractor=GatedExtractor(events),
            recognizer=FakeRecognizer(events),
            lifecycle=FakeLifecycle(events),
            synthesizer=GatedSynthesizer(events),
            faster_whisper_recognizer=FakeFasterWhisper(events),
        )

        await preparer.start()
        await asyncio.wait_for(kokoro_started.wait(), timeout=1)
        self.assertTrue(await asyncio.to_thread(qwen_started.wait, 1))
        self.assertEqual((await preparer.status())["state"], "PREPARING")

        qwen_gate.set()
        kokoro_gate.set()
        result = await preparer.wait()

        self.assertEqual(result["state"], "READY")
        self.assertLess(
            events.index("extractor.extract"), events.index("synthesizer.finished")
        )

    async def test_final_residency_snapshot_must_confirm_all_four_workers(self):
        events = []
        initial = gpu_snapshot(
            service("nemotron"), service("llama"), service("kokoro")
        )
        kokoro_evicted = gpu_snapshot(
            service("nemotron"),
            service("llama"),
            service("kokoro", state="idle", worker_present=False),
        )
        preparer = coordinator(events, snapshots=[initial, kokoro_evicted])

        await preparer.start()
        result = await preparer.wait()

        self.assertEqual(result["state"], "FAILED")
        kokoro = next(stage for stage in result["stages"] if stage["name"] == "kokoro")
        self.assertEqual(kokoro["state"], "FAILED")
        self.assertEqual(kokoro["reason_code"], "KOKORO_UNAVAILABLE")

    async def test_missing_faster_whisper_worker_prevents_ready(self):
        events = []
        missing = gpu_snapshot(
            service("nemotron"),
            service("llama"),
            service("kokoro"),
            service("faster_whisper", state="idle", worker_present=False),
        )
        preparer = coordinator(events, snapshots=[missing])

        await preparer.start()
        result = await preparer.wait()

        self.assertEqual(result["state"], "FAILED")
        faster = next(
            stage for stage in result["stages"] if stage["name"] == "faster_whisper"
        )
        self.assertEqual(faster["reason_code"], "FASTER_WHISPER_UNAVAILABLE")

    async def test_preparation_runs_capacity_nemotron_qwen_kokoro_in_order(self):
        events = []
        preparer = coordinator(events)

        started = await preparer.start()
        result = await preparer.wait()

        self.assertEqual(started["state"], "PREPARING")
        self.assertEqual(result["state"], "READY")
        self.assertIs(result["best_effort"], False)
        self.assertEqual(
            events[:5],
            [
                "status",
                "lifecycle.acquire",
                "recognizer.open",
                "stream.close",
                "lifecycle.release",
            ],
        )
        self.assertCountEqual(
            events[5:8],
            ["extractor.extract", "synthesizer", "faster_whisper.transcribe"],
        )
        self.assertEqual(events[8:], ["status"])
        self.assertEqual(
            [stage["name"] for stage in result["stages"]],
            ["capacity", "nemotron", "qwen", "kokoro", "faster_whisper"],
        )
        self.assertEqual(
            [worker["name"] for worker in result["workers"]],
            ["qwen", "nemotron", "kokoro", "faster_whisper"],
        )
        self.assertTrue(all(worker["worker_pid"] for worker in result["workers"]))
        self.assertEqual(result["gpu"]["total_mb"], 12_288)
        ready_at = datetime.fromisoformat(result["completed_at"])
        ready_until = datetime.fromisoformat(result["ready_until"])
        self.assertEqual((ready_until - ready_at).total_seconds(), 60)

    async def test_active_start_returns_the_same_run_without_duplicate_calls(self):
        events = []
        gate = asyncio.Event()
        preparer = coordinator(events, gate=gate)

        first = await preparer.start()
        await asyncio.sleep(0)
        second = await preparer.start()
        gate.set()
        result = await preparer.wait()

        self.assertEqual(first["started_at"], second["started_at"])
        self.assertEqual(result["state"], "READY")
        self.assertEqual(events.count("status"), 2)
        self.assertEqual(events.count("synthesizer"), 1)

    async def test_whisper_busy_fails_before_warming_any_provider(self):
        events = []
        preparer = coordinator(
            events,
            snapshots=[
                gpu_snapshot(
                    service("whisper", state="busy", active_jobs=1),
                    service("nemotron"),
                    service("llama"),
                    service("kokoro"),
                )
            ],
        )

        await preparer.start()
        result = await preparer.wait()

        self.assertEqual(events, ["status"])
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(result["stages"][0]["reason_code"], "WHISPER_BUSY")

    async def test_missing_whisper_record_allows_preparation(self):
        events = []
        preparer = coordinator(
            events,
            snapshots=[
                gpu_snapshot(
                    service("nemotron"), service("llama"), service("kokoro")
                )
            ],
        )

        await preparer.start()
        result = await preparer.wait()

        self.assertEqual(result["state"], "READY")
        self.assertIn("synthesizer", events)

    async def test_each_stage_timeout_maps_to_its_stable_reason_code(self):
        class HangingStatusClient:
            async def read(self):
                await asyncio.Event().wait()

        class HangingLifecycle(FakeLifecycle):
            async def acquire(self):
                await asyncio.Event().wait()

        class SlowExtractor(FakeExtractor):
            def extract(self, text):
                time.sleep(0.05)
                return object()

        class HangingSynthesizer(FakeSynthesizer):
            async def __call__(self, text):
                await asyncio.Event().wait()

        class HangingFasterWhisper(FakeFasterWhisper):
            async def transcribe(self, audio, on_partial):
                await asyncio.Event().wait()

        for timed_out_stage in ("capacity", "nemotron", "qwen", "kokoro", "faster_whisper"):
            with self.subTest(stage=timed_out_stage):
                events = []
                preparer = DemoPreparationCoordinator(
                    status_client=(
                        HangingStatusClient()
                        if timed_out_stage == "capacity"
                        else FakeStatusClient(events)
                    ),
                    extractor=(
                        SlowExtractor(events)
                        if timed_out_stage == "qwen"
                        else FakeExtractor(events)
                    ),
                    recognizer=FakeRecognizer(events),
                    lifecycle=(
                        HangingLifecycle(events)
                        if timed_out_stage == "nemotron"
                        else FakeLifecycle(events)
                    ),
                    synthesizer=(
                        HangingSynthesizer(events)
                        if timed_out_stage == "kokoro"
                        else FakeSynthesizer(events)
                    ),
                    faster_whisper_recognizer=(
                        HangingFasterWhisper(events)
                        if timed_out_stage == "faster_whisper"
                        else FakeFasterWhisper(events)
                    ),
                    capacity_timeout_seconds=0.01,
                    nemotron_timeout_seconds=0.01,
                    qwen_timeout_seconds=0.01,
                    kokoro_timeout_seconds=0.01,
                    faster_whisper_timeout_seconds=0.01,
                )

                await preparer.start()
                result = await preparer.wait()

                stage = next(
                    item for item in result["stages"] if item["name"] == timed_out_stage
                )
                self.assertEqual(result["state"], "FAILED")
                self.assertEqual(stage["state"], "FAILED")
                self.assertEqual(stage["reason_code"], "PREPARATION_TIMEOUT")

    async def test_preparation_releases_its_handle_on_success_failure_and_cancel(self):
        success_events = []
        successful = coordinator(success_events)
        await successful.start()
        await successful.wait()
        self.assertEqual(success_events.count("lifecycle.release"), 1)

        class FailingRecognizer(FakeRecognizer):
            async def open_stream(self, on_partial):
                self.events.append("recognizer.open")
                raise RuntimeError("private recognizer failure")

        failure_events = []
        failed = DemoPreparationCoordinator(
            status_client=FakeStatusClient(failure_events),
            extractor=FakeExtractor(failure_events),
            recognizer=FailingRecognizer(failure_events),
            lifecycle=FakeLifecycle(failure_events),
            synthesizer=FakeSynthesizer(failure_events),
            faster_whisper_recognizer=FakeFasterWhisper(failure_events),
        )
        await failed.start()
        failure_result = await failed.wait()
        self.assertEqual(failure_result["state"], "FAILED")
        self.assertEqual(failure_events.count("lifecycle.release"), 1)

        entered = asyncio.Event()

        class BlockingRecognizer(FakeRecognizer):
            async def open_stream(self, on_partial):
                self.events.append("recognizer.open")
                entered.set()
                await asyncio.Event().wait()

        cancellation_events = []
        cancelled = DemoPreparationCoordinator(
            status_client=FakeStatusClient(cancellation_events),
            extractor=FakeExtractor(cancellation_events),
            recognizer=BlockingRecognizer(cancellation_events),
            lifecycle=FakeLifecycle(cancellation_events),
            synthesizer=FakeSynthesizer(cancellation_events),
            faster_whisper_recognizer=FakeFasterWhisper(cancellation_events),
        )
        await cancelled.start()
        await entered.wait()
        cancelled._task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled._task
        self.assertEqual(cancellation_events.count("lifecycle.release"), 1)

        class BrokenCloseStream(FakeStream):
            async def close(self):
                self.events.append("stream.close.failed")
                raise RuntimeError("private stream close failure")

        class BrokenCloseRecognizer(FakeRecognizer):
            async def open_stream(self, on_partial):
                self.events.append("recognizer.open")
                return BrokenCloseStream(self.events)

        close_failure_events = []
        close_failed = DemoPreparationCoordinator(
            status_client=FakeStatusClient(close_failure_events),
            extractor=FakeExtractor(close_failure_events),
            recognizer=BrokenCloseRecognizer(close_failure_events),
            lifecycle=FakeLifecycle(close_failure_events),
            synthesizer=FakeSynthesizer(close_failure_events),
            faster_whisper_recognizer=FakeFasterWhisper(close_failure_events),
        )
        await close_failed.start()
        close_failure_result = await close_failed.wait()
        self.assertEqual(close_failure_result["state"], "FAILED")
        self.assertEqual(close_failure_events.count("lifecycle.release"), 1)

    async def test_stop_cancels_in_progress_preparation_and_marks_running_stage(self):
        events = []
        gate = asyncio.Event()
        preparer = coordinator(events, gate=gate)

        await preparer.start()
        await asyncio.sleep(0)
        await preparer.stop()

        result = await preparer.status()
        self.assertEqual(result["state"], "FAILED")
        capacity = next(stage for stage in result["stages"] if stage["name"] == "capacity")
        self.assertEqual(capacity["state"], "FAILED")
        self.assertEqual(capacity["reason_code"], "PREPARATION_CANCELLED")

    async def test_qwen_and_kokoro_warmup_do_not_touch_database_or_files(self):
        events = []
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with chdir(root):
                preparer = coordinator(events)
                await preparer.start()
                result = await preparer.wait()

            self.assertEqual(result["state"], "READY")
            self.assertEqual(list(root.iterdir()), [])

    async def test_missing_prepared_worker_changes_ready_to_degraded(self):
        events = []
        warm = gpu_snapshot(
            service("whisper", state="idle"),
            service("nemotron"),
            service("llama"),
            service("kokoro"),
        )
        warm_after_probes = gpu_snapshot(
            service("whisper"),
            service("nemotron"),
            service("llama"),
            service("kokoro"),
        )
        qwen_evicted = gpu_snapshot(
            service("whisper"),
            service("nemotron"),
            service("llama", state="idle", worker_present=False),
            service("kokoro"),
        )
        preparer = coordinator(events, snapshots=[warm, warm_after_probes, qwen_evicted])

        await preparer.start()
        ready = await preparer.wait()
        degraded = await preparer.status()

        self.assertEqual(ready["state"], "READY")
        self.assertEqual(degraded["state"], "DEGRADED")
        qwen = next(stage for stage in degraded["stages"] if stage["name"] == "qwen")
        self.assertEqual(qwen["reason_code"], "QWEN_UNAVAILABLE")

    async def test_raw_upstream_exception_text_is_not_exposed(self):
        class ExplodingExtractor(FakeExtractor):
            def extract(self, text):
                raise RuntimeError("token=private /secret/socket/path")

        events = []
        preparer = DemoPreparationCoordinator(
            status_client=FakeStatusClient(events),
            extractor=ExplodingExtractor(events),
            recognizer=FakeRecognizer(events),
            lifecycle=FakeLifecycle(events),
            synthesizer=FakeSynthesizer(events),
            faster_whisper_recognizer=FakeFasterWhisper(events),
        )

        await preparer.start()
        result = await preparer.wait()
        encoded = json.dumps(result)

        self.assertEqual(result["state"], "FAILED")
        self.assertNotIn("private", encoded)
        self.assertNotIn("socket", encoded)
        qwen = next(stage for stage in result["stages"] if stage["name"] == "qwen")
        self.assertEqual(qwen["reason_code"], "QWEN_UNAVAILABLE")

    async def test_qwen_warmup_rejects_non_intent_return(self):
        class InvalidExtractor(FakeExtractor):
            def extract(self, text):
                return object()

        events = []
        preparer = DemoPreparationCoordinator(
            status_client=FakeStatusClient(events),
            extractor=InvalidExtractor(events),
            recognizer=FakeRecognizer(events),
            lifecycle=FakeLifecycle(events),
            synthesizer=FakeSynthesizer(events),
            faster_whisper_recognizer=FakeFasterWhisper(events),
        )

        await preparer.start()
        result = await preparer.wait()

        self.assertEqual(result["state"], "FAILED")
        qwen = next(stage for stage in result["stages"] if stage["name"] == "qwen")
        self.assertEqual(qwen["reason_code"], "QWEN_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
