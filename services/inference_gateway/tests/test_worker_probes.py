from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from apps.dimon_live.gpu_overlord_status import GpuServiceStatus, GpuStatusSnapshot
from services.inference_gateway.residency import ProbeEvidence
from services.inference_gateway.worker_probes import (
    FasterWhisperRuntimeProbe,
    QwenRuntimeProbe,
)


def response(payload: object, *, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(payload).encode(),
        request=httpx.Request("GET", "http://127.0.0.1/probe"),
    )


class Requester:
    def __init__(self, result: httpx.Response | Exception) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, method: str, url: str) -> httpx.Response:
        self.calls.append((method, url))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class StatusClient:
    def __init__(self, *, pid: int = 501, profile: str = "qwen35-9b-abliterated-q5-4k") -> None:
        self.pid = pid
        self.profile = profile

    async def read(self) -> GpuStatusSnapshot:
        return GpuStatusSnapshot(
            total_mb=12_282,
            free_mb=2_000,
            safety_reserve_mb=512,
            services=(
                GpuServiceStatus(
                    service="llama",
                    state="ready",
                    active_jobs=0,
                    worker_pid=self.pid,
                    profile=self.profile,
                    budget_mb=6_528,
                    observed_vram_mb=5_852,
                    heartbeat_age_seconds=0.1,
                ),
            ),
        )


class WorkerProbeTests(unittest.TestCase):
    def test_qwen_probe_requires_q5_model_specific_props_and_4096_context(self) -> None:
        requester = Requester(
            response(
                {
                    "model_path": "/private/Huihui-Qwen3.5-9B-abliterated.Q5_K_M.gguf",
                    "default_generation_settings": {"n_ctx": 4096, "params": {}},
                    "secret": "discard",
                }
            )
        )
        probe = QwenRuntimeProbe(
            "http://127.0.0.1:8080/v1",
            "qwen35-9b-abliterated-q5",
            StatusClient(),
            requester=requester,
        )

        result = asyncio.run(probe())

        self.assertEqual(
            result,
            ProbeEvidence(
                name="qwen",
                worker_pid=501,
                ok=True,
                reason_code="READY",
                model="qwen35-9b-abliterated-q5",
                device="cuda",
                compute_type="Q5_K_M",
                context_tokens=4096,
            ),
        )
        self.assertEqual(
            requester.calls,
            [("GET", "http://127.0.0.1:8080/props?model=qwen35-9b-abliterated-q5")],
        )
        self.assertNotIn("private", repr(result).lower())

    def test_qwen_probe_rejects_wrong_context_quant_profile_and_malformed_response(self) -> None:
        cases = (
            ({"model_path": "model.Q5_K_M.gguf", "default_generation_settings": {"n_ctx": 8192}}, "QWEN_IDENTITY_MISMATCH"),
            ({"model_path": "model.Q4_K_M.gguf", "default_generation_settings": {"n_ctx": 4096}}, "QWEN_IDENTITY_MISMATCH"),
            ({"model_path": "model.Q5_K_M.gguf", "default_generation_settings": []}, "QWEN_IDENTITY_MISMATCH"),
        )
        for payload, reason in cases:
            with self.subTest(reason=reason, payload=payload):
                result = asyncio.run(
                    QwenRuntimeProbe(
                        "http://127.0.0.1:8080/v1",
                        "qwen35-9b-abliterated-q5",
                        StatusClient(),
                        requester=Requester(response(payload)),
                    )()
                )
                self.assertFalse(result.ok)
                self.assertEqual(result.reason_code, reason)

        wrong_profile = asyncio.run(
            QwenRuntimeProbe(
                "http://127.0.0.1:8080/v1",
                "qwen35-9b-abliterated-q5",
                StatusClient(profile="wrong"),
                requester=Requester(
                    response(
                        {
                            "model_path": "model.Q5_K_M.gguf",
                            "default_generation_settings": {"n_ctx": 4096},
                        }
                    )
                ),
            )()
        )
        self.assertFalse(wrong_profile.ok)
        self.assertEqual(wrong_profile.reason_code, "QWEN_IDENTITY_MISMATCH")

    def test_medium_probe_requires_loaded_cuda_medium_identity_and_positive_pid(self) -> None:
        requester = Requester(
            response(
                {
                    "status": "ready",
                    "ready": True,
                    "loaded": True,
                    "worker_pid": 777,
                    "model": "medium",
                    "device": "cuda",
                    "compute_type": "int8_float16",
                    "secret": "discard",
                }
            )
        )
        result = asyncio.run(
            FasterWhisperRuntimeProbe(
                "ws://127.0.0.1:8007/ws", requester=requester
            )()
        )

        self.assertEqual(
            result,
            ProbeEvidence(
                name="faster_whisper",
                worker_pid=777,
                ok=True,
                reason_code="READY",
                model="medium",
                device="cuda",
                compute_type="int8_float16",
                context_tokens=None,
            ),
        )
        self.assertEqual(requester.calls, [("POST", "http://127.0.0.1:8007/probe")])

    def test_medium_probe_and_transport_failures_return_only_bounded_codes(self) -> None:
        invalid = {
            "ready": True,
            "loaded": True,
            "worker_pid": 0,
            "model": "small",
            "device": "cpu",
            "compute_type": "int8",
        }
        identity = asyncio.run(
            FasterWhisperRuntimeProbe(
                "ws://127.0.0.1:8007/ws",
                requester=Requester(response(invalid)),
            )()
        )
        unavailable = asyncio.run(
            FasterWhisperRuntimeProbe(
                "ws://127.0.0.1:8007/ws",
                requester=Requester(httpx.ReadTimeout("private endpoint")),
            )()
        )

        self.assertEqual(identity.reason_code, "FASTER_WHISPER_IDENTITY_MISMATCH")
        self.assertEqual(unavailable.reason_code, "FASTER_WHISPER_UNAVAILABLE")
        self.assertNotIn("private", repr(unavailable).lower())


if __name__ == "__main__":
    unittest.main()
