from __future__ import annotations

import unittest
from uuid import uuid4

from apps.dimon_live.voice_trace import VoiceTraceRejected, VoiceTraceStore


class Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


class VoiceTraceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.store = VoiceTraceStore(monotonic=self.clock)
        self.trace_id = str(uuid4())
        self.attempt_id = str(uuid4())

    def test_records_allowlisted_browser_and_server_milestones_without_content(self) -> None:
        self.store.start(self.trace_id)
        self.store.record_browser(self.trace_id, "d_click", 0.0)
        self.store.start_attempt(self.trace_id, self.attempt_id)
        self.clock.value = 100.125
        self.store.record_server(
            self.trace_id,
            "speech_detected",
            attempt_id=self.attempt_id,
        )
        self.store.finish_attempt(
            self.trace_id,
            self.attempt_id,
            path="nemotron_partial",
        )

        latest = self.store.latest()

        self.assertEqual(latest["trace_id"], self.trace_id)
        self.assertEqual(latest["attempts"][0]["attempt_id"], self.attempt_id)
        self.assertEqual(latest["attempts"][0]["path"], "nemotron_partial")
        self.assertEqual(
            [(item["source"], item["name"]) for item in latest["events"]],
            [
                ("browser", "d_click"),
                ("server", "attempt_started"),
                ("server", "speech_detected"),
                ("server", "attempt_finished"),
            ],
        )
        self.assertEqual(latest["events"][2]["elapsed_ms"], 125.0)
        encoded = repr(latest).lower()
        for forbidden in ("audio", "transcript", "prompt", "payee", "payment", "reply"):
            self.assertNotIn(forbidden, encoded)

    def test_rejects_non_uuid4_ids_unknown_events_negative_time_and_content_fields(self) -> None:
        self.store.start(self.trace_id)
        invalid_calls = (
            lambda: self.store.start("trace-1"),
            lambda: self.store.start("00000000-0000-1000-8000-000000000000"),
            lambda: self.store.record_browser(self.trace_id, "typed_text", 1),
            lambda: self.store.record_browser(self.trace_id, "audio_ready", -1),
            lambda: self.store.record_browser(
                self.trace_id, "audio_ready", 1, transcript="secret"
            ),
            lambda: self.store.record_server(
                self.trace_id, "first_partial", prompt="private"
            ),
            lambda: self.store.start_attempt(self.trace_id, "attempt-1"),
        )
        for call in invalid_calls:
            with self.subTest(call=call), self.assertRaises(VoiceTraceRejected):
                call()

    def test_bounds_traces_events_and_attempts_without_mixing_attempt_ids(self) -> None:
        trace_ids = [str(uuid4()) for _ in range(33)]
        for trace_id in trace_ids:
            self.store.start(trace_id)
        self.assertEqual(self.store.trace_count, 32)

        latest_id = trace_ids[-1]
        first_attempt = str(uuid4())
        second_attempt = str(uuid4())
        self.store.start_attempt(latest_id, first_attempt)
        self.store.start_attempt(latest_id, second_attempt)
        for index in range(70):
            self.store.record_browser(latest_id, "audio_ready", float(index))
        self.store.finish_attempt(
            latest_id, first_attempt, path="faster_whisper_final"
        )
        self.store.finish_attempt(
            latest_id,
            second_attempt,
            path="failed",
            failure_phase="nemotron_ready_timeout",
        )

        latest = self.store.latest()
        self.assertLessEqual(len(latest["events"]), 64)
        by_id = {item["attempt_id"]: item for item in latest["attempts"]}
        self.assertEqual(by_id[first_attempt]["path"], "faster_whisper_final")
        self.assertIsNone(by_id[first_attempt]["failure_phase"])
        self.assertEqual(by_id[second_attempt]["path"], "failed")
        self.assertEqual(
            by_id[second_attempt]["failure_phase"], "nemotron_ready_timeout"
        )

    def test_unknown_trace_attempt_path_and_failure_phase_fail_closed(self) -> None:
        self.store.start(self.trace_id)
        self.store.start_attempt(self.trace_id, self.attempt_id)
        cases = (
            lambda: self.store.record_server(str(uuid4()), "speech_detected"),
            lambda: self.store.finish_attempt(
                self.trace_id, str(uuid4()), path="nemotron_partial"
            ),
            lambda: self.store.finish_attempt(
                self.trace_id, self.attempt_id, path="cloud_unknown"
            ),
            lambda: self.store.finish_attempt(
                self.trace_id,
                self.attempt_id,
                path="failed",
                failure_phase="token=/private/path",
            ),
        )
        for call in cases:
            with self.subTest(call=call), self.assertRaises(VoiceTraceRejected):
                call()

    def test_successful_cuda_fallback_keeps_the_bounded_nemotron_failure_phase(self) -> None:
        self.store.start(self.trace_id)
        self.store.start_attempt(self.trace_id, self.attempt_id)

        self.store.finish_attempt(
            self.trace_id,
            self.attempt_id,
            path="faster_whisper_final",
            failure_phase="nemotron_connect",
        )

        attempt = self.store.latest()["attempts"][0]
        self.assertEqual(attempt["path"], "faster_whisper_final")
        self.assertEqual(attempt["failure_phase"], "nemotron_connect")

    def test_attempt_keeps_content_free_recognizer_quality_metadata(self) -> None:
        self.store.start(self.trace_id)
        self.store.start_attempt(self.trace_id, self.attempt_id)

        self.store.finish_attempt(
            self.trace_id,
            self.attempt_id,
            path="nemotron_final",
            recognizer_disagreement=True,
            faster_whisper_confidence="low",
            faster_whisper_avg_logprob=-0.48,
            faster_whisper_no_speech_prob=0.38,
        )

        attempt = self.store.latest()["attempts"][0]
        self.assertEqual(attempt["path"], "nemotron_final")
        self.assertTrue(attempt["recognizer_disagreement"])
        self.assertEqual(attempt["faster_whisper_confidence"], "low")

    def test_degraded_partial_completion_keeps_nemotron_failure_phase(self) -> None:
        self.store.start(self.trace_id)
        self.store.start_attempt(self.trace_id, self.attempt_id)

        self.store.finish_attempt(
            self.trace_id,
            self.attempt_id,
            path="nemotron_partial",
            failure_phase="nemotron_stream_closed",
        )

        attempt = self.store.latest()["attempts"][0]
        self.assertEqual(attempt["path"], "nemotron_partial")
        self.assertEqual(attempt["failure_phase"], "nemotron_stream_closed")


if __name__ == "__main__":
    unittest.main()
