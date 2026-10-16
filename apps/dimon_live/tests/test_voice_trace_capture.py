from __future__ import annotations

import json
import unittest
from uuid import uuid4


class VoiceTraceCaptureTests(unittest.TestCase):
    def payload(
        self,
        *,
        path: str = "nemotron_partial",
        failure_phase: str | None = None,
        phases: tuple[tuple[str, str, float], ...] | None = None,
    ) -> dict[str, object]:
        from scripts.capture_voice_trace import EXPECTED_PHASES

        if phases is None:
            phases = tuple(
                (
                    "browser" if name in {
                        "d_click",
                        "audio_ready",
                        "microphone_ready",
                        "socket_ready",
                        "backend_ready",
                        "browser_wake_received",
                        "reply_audio_ready",
                        "reply_playback_started",
                    } else "server",
                    name,
                    float(index * 100),
                )
                for index, name in enumerate(EXPECTED_PHASES)
            )
        return {
            "trace": {
                "trace_id": str(uuid4()),
                "started_at": "2026-10-15T01:00:00+00:00",
                "events": [
                    {"source": source, "name": name, "elapsed_ms": elapsed_ms}
                    for source, name, elapsed_ms in phases
                ],
                "attempts": [{
                    "attempt_id": str(uuid4()),
                    "path": path,
                    "failure_phase": failure_phase,
                }],
            },
            "workers": [
                {
                    "name": name,
                    "state": "READY",
                    "loaded": True,
                    "worker_pid": index + 100,
                    "active_jobs": 0,
                    "observed_vram_mb": 1000 + index,
                    "model": name,
                    "device": "cuda",
                    "compute_type": "float16",
                    "context_tokens": 4096 if name == "qwen" else None,
                }
                for index, name in enumerate(
                    ("qwen", "nemotron", "kokoro", "faster_whisper")
                )
            ],
            "gpu": {"total_mb": 12282, "free_mb": 2213, "safety_reserve_mb": 512},
        }

    def test_complete_trace_scores_every_expected_phase(self) -> None:
        from scripts.capture_voice_trace import capture_record

        payload = self.payload(path="nemotron_final")
        payload["trace"]["attempts"][0].update({
            "recognizer_disagreement": True,
            "faster_whisper_confidence": "low",
            "faster_whisper_avg_logprob": -0.48,
            "faster_whisper_no_speech_prob": 0.38,
        })
        record = capture_record(payload)

        self.assertEqual(record["completeness"]["ratio"], 1.0)
        self.assertEqual(record["completeness"]["missing"], [])
        self.assertEqual(record["recognition_path"], "nemotron_final")
        self.assertEqual(record["failure_phase"], None)
        self.assertTrue(record["recognizer_disagreement"])
        self.assertEqual(record["faster_whisper_confidence"], "low")
        self.assertEqual(set(record["timings_ms"]), {"browser", "server"})

    def test_partial_trace_reports_missing_phases_without_fabricating_values(self) -> None:
        from scripts.capture_voice_trace import capture_record

        record = capture_record(self.payload(phases=(
            ("browser", "d_click", 0),
            ("server", "speech_detected", 240),
        )))

        self.assertLess(record["completeness"]["ratio"], 1.0)
        self.assertIn("reply_playback_started", record["completeness"]["missing"])
        self.assertNotIn("reply_playback_started", record["timings_ms"]["browser"])

    def test_negative_and_cross_domain_durations_are_rejected(self) -> None:
        from scripts.capture_voice_trace import CaptureRejected, capture_record

        payload = self.payload(phases=(
            ("browser", "d_click", 0),
            ("server", "speech_detected", -1),
        ))
        with self.assertRaisesRegex(CaptureRejected, "elapsed"):
            capture_record(payload)

        record = capture_record(self.payload(phases=(
            ("browser", "d_click", 9000),
            ("server", "speech_detected", 100),
        )))
        self.assertEqual(record["timings_ms"]["browser"]["d_click"], 9000)
        self.assertEqual(record["timings_ms"]["server"]["speech_detected"], 100)
        self.assertNotIn("cross_domain_delta_ms", record)

    def test_percentiles_and_fallback_grouping_are_deterministic(self) -> None:
        from scripts.capture_voice_trace import (
            capture_record,
            percentile,
            summarize_records,
        )

        self.assertEqual(percentile([1, 2, 3, 4], 50), 2.5)
        self.assertEqual(percentile([1, 2, 3, 4], 95), 3.85)
        records = [
            capture_record(self.payload()),
            capture_record(self.payload(
                path="faster_whisper_final",
                failure_phase="nemotron_ready_timeout",
            )),
            capture_record(self.payload(
                path="faster_whisper_final",
                failure_phase="nemotron_connect",
            )),
        ]

        summary = summarize_records(records)

        self.assertEqual(summary["recognition_paths"], {
            "faster_whisper_final": 2,
            "nemotron_partial": 1,
        })
        self.assertEqual(summary["failure_phases"], {
            "nemotron_connect": 1,
            "nemotron_ready_timeout": 1,
        })
        self.assertEqual(summary["trials"], 3)
        self.assertIn("reply_playback_started", summary["timings_ms"]["browser"])

    def test_content_fields_are_refused_instead_of_serialized(self) -> None:
        from scripts.capture_voice_trace import CaptureRejected, build_report, capture_record

        for key, value in (
            ("transcript", "Hey Dimon pay Mallory"),
            ("audio", "private bytes"),
            ("prompt", "send money"),
            ("url", "ws://private.example"),
        ):
            payload = self.payload()
            payload["trace"][key] = value
            with self.assertRaises(CaptureRejected):
                capture_record(payload)

        record = capture_record(self.payload())
        encoded = json.dumps(build_report([record]), sort_keys=True).lower()
        for forbidden in (
            '"transcript":',
            '"audio":',
            '"prompt":',
            '"payee":',
            "private bytes",
            "ws://",
            "/home/",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_collector_waits_for_a_new_terminal_trace(self) -> None:
        from scripts.capture_voice_trace import collect_trials

        baseline = self.payload()
        partial = self.payload(phases=(("browser", "d_click", 0),))
        complete = self.payload()
        complete["trace"]["trace_id"] = partial["trace"]["trace_id"]
        responses = iter((baseline, partial, complete))
        clock = [0.0]

        records = collect_trials(
            lambda: next(responses),
            count=1,
            poll_interval=0.25,
            trial_timeout=5,
            settle_seconds=0,
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            monotonic=lambda: clock[0],
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["trace_id"], partial["trace"]["trace_id"])
        self.assertEqual(records[0]["completeness"]["ratio"], 1.0)

    def test_capture_uses_one_terminal_attempt_without_mixing_earlier_events(self) -> None:
        from scripts.capture_voice_trace import capture_record

        payload = self.payload()
        first_id = str(uuid4())
        final_id = str(uuid4())
        payload["trace"]["attempts"] = [
            {"attempt_id": first_id, "path": "failed", "failure_phase": "wake_not_matched"},
            {"attempt_id": final_id, "path": "nemotron_partial", "failure_phase": None},
        ]
        payload["trace"]["events"] = [
            {"source": "browser", "name": "d_click", "elapsed_ms": 0},
            {"source": "server", "name": "speech_detected", "elapsed_ms": 200, "attempt_id": first_id},
            {"source": "server", "name": "speech_detected", "elapsed_ms": 5200, "attempt_id": final_id},
            {"source": "browser", "name": "reply_playback_started", "elapsed_ms": 5700, "attempt_id": final_id},
        ]

        record = capture_record(payload)

        self.assertEqual(record["selected_attempt_id"], final_id)
        self.assertEqual(record["timings_ms"]["server"]["speech_detected"], 5200)
        self.assertNotIn(first_id, {event.get("attempt_id") for event in record["events"]})

    def test_successful_attempt_is_not_terminal_until_reply_playback_starts(self) -> None:
        from scripts.capture_voice_trace import _trace_terminal, capture_record

        payload = self.payload(phases=(("browser", "d_click", 0),))
        self.assertFalse(_trace_terminal(capture_record(payload)))

        payload["trace"]["attempts"][0] = {
            "attempt_id": str(uuid4()),
            "path": "failed",
            "failure_phase": "wake_not_matched",
        }
        self.assertTrue(_trace_terminal(capture_record(payload)))


if __name__ == "__main__":
    unittest.main()
