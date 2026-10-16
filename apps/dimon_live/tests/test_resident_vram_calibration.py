from __future__ import annotations

import json
import unittest

from scripts.calibrate_resident_vram import (
    WorkerEvidence,
    build_report,
    calculate_budget_mb,
    parse_compute_apps,
    validate_evidence,
    validate_reserve,
)


def evidence() -> tuple[WorkerEvidence, ...]:
    return (
        WorkerEvidence("qwen", "llama", 101, "qwen35-9b-abliterated-q5-4k", "qwen35-9b-abliterated-q5", "cuda", "Q5_K_M"),
        WorkerEvidence("nemotron", "nemotron", 202, "nemotron-en", "nemotron-en", "cuda", "fp16"),
        WorkerEvidence("kokoro", "kokoro", 303, "kokoro-82m", "kokoro-82m", "cuda", "fp32"),
        WorkerEvidence("faster_whisper", "faster_whisper", 404, "faster-whisper-medium-cuda", "medium", "cuda", "int8_float16"),
    )


class ResidentVramCalibrationTests(unittest.TestCase):
    def test_process_samples_are_bound_to_verified_worker_pids(self) -> None:
        parsed = parse_compute_apps(
            "101, 5846\n202, 1226\n303, 810\n404, 1510\n999, 99\n",
            {101, 202, 303, 404},
        )

        self.assertEqual(parsed, {101: 5846, 202: 1226, 303: 810, 404: 1510})

    def test_missing_or_reused_pid_fails_calibration(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing worker PID"):
            parse_compute_apps("101, 5846\n202, 1226\n", {101, 202, 303})

        duplicate = list(evidence())
        duplicate[-1] = WorkerEvidence(
            "faster_whisper",
            "faster_whisper",
            303,
            "faster-whisper-medium-cuda",
            "medium",
            "cuda",
            "int8_float16",
        )
        with self.assertRaisesRegex(ValueError, "distinct worker PID"):
            validate_evidence(tuple(duplicate))

    def test_budget_uses_peak_plus_headroom_rounded_to_128_mib(self) -> None:
        self.assertEqual(calculate_budget_mb(5846, 5900), 6528)
        self.assertEqual(calculate_budget_mb(1000, 1100), 1408)
        self.assertEqual(calculate_budget_mb(1500, 1200), 1792)

    def test_report_rejects_wrong_worker_identity(self) -> None:
        wrong = list(evidence())
        wrong[-1] = WorkerEvidence(
            "faster_whisper",
            "faster_whisper",
            404,
            "faster-whisper-medium-cuda",
            "medium",
            "cpu",
            "int8",
        )

        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            validate_evidence(tuple(wrong))

    def test_report_contains_no_request_or_transcript_content(self) -> None:
        workers = evidence()
        samples = {
            item.name: {"loaded_idle_mb": 1000, "inference_peak_mb": 1200}
            for item in workers
        }

        report = build_report(
            workers,
            samples,
            gpu_total_mb=12282,
            final_free_mb=1024,
            safety_reserve_mb=512,
            measured_at="2026-10-14T20:00:00+08:00",
        )
        encoded = json.dumps(report, sort_keys=True).lower()

        self.assertNotIn("transcript", encoded)
        self.assertNotIn("prompt", encoded)
        self.assertNotIn("audio", encoded)
        self.assertNotIn("payee", encoded)
        self.assertEqual(len(report["workers"]), 4)

    def test_four_worker_acceptance_requires_512_mib_free(self) -> None:
        validate_reserve(total_mb=12282, free_mb=512, required_mb=512)

        with self.assertRaisesRegex(ValueError, "VRAM reserve"):
            validate_reserve(total_mb=12282, free_mb=511, required_mb=512)


if __name__ == "__main__":
    unittest.main()
