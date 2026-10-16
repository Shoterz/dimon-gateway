"""Bounded real probes for the Qwen and CUDA Medium worker identities."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import PurePath
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from apps.dimon_live.gpu_overlord_status import GpuStatusError

from .residency import ProbeEvidence


Requester = Callable[[str, str], Awaitable[httpx.Response]]
_MAX_RESPONSE_BYTES = 65_536


async def _default_request(method: str, url: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        return await client.request(method, url)


def _json_object(response: httpx.Response) -> dict[str, object]:
    if response.status_code < 200 or response.status_code >= 300:
        raise ValueError("probe failed")
    if len(response.content) > _MAX_RESPONSE_BYTES:
        raise ValueError("probe response is too large")
    value = response.json()
    if not isinstance(value, dict):
        raise ValueError("probe response is invalid")
    return value


class QwenRuntimeProbe:
    def __init__(
        self,
        api_base: str,
        model: str,
        status_client,
        *,
        requester: Requester | None = None,
    ) -> None:
        parsed = urlsplit(api_base)
        self.url = urlunsplit(
            (parsed.scheme, parsed.netloc, "/props", urlencode({"model": model}), "")
        )
        self.model = model
        self.status_client = status_client
        self.requester = requester or _default_request

    async def __call__(self) -> ProbeEvidence:
        try:
            payload = _json_object(await self.requester("GET", self.url))
            settings = payload.get("default_generation_settings")
            model_path = payload.get("model_path")
            if (
                not isinstance(settings, dict)
                or settings.get("n_ctx") != 4096
                or not isinstance(model_path, str)
                or len(model_path) > 4096
                or not PurePath(model_path).name.lower().endswith(".q5_k_m.gguf")
            ):
                return self._failure("QWEN_IDENTITY_MISMATCH")
            status = await self.status_client.read()
            worker = status.service("llama")
            if (
                worker is None
                or worker.worker_pid is None
                or worker.worker_pid <= 0
                or worker.profile != "qwen35-9b-abliterated-q5-4k"
                or worker.state not in {"ready", "busy"}
            ):
                return self._failure("QWEN_IDENTITY_MISMATCH")
            return ProbeEvidence(
                name="qwen",
                worker_pid=worker.worker_pid,
                ok=True,
                reason_code="READY",
                model=self.model,
                device="cuda",
                compute_type="Q5_K_M",
                context_tokens=4096,
            )
        except (httpx.HTTPError, GpuStatusError, ValueError, TypeError):
            return self._failure("QWEN_UNAVAILABLE")

    @staticmethod
    def _failure(reason_code: str) -> ProbeEvidence:
        return ProbeEvidence("qwen", 0, False, reason_code)


class FasterWhisperRuntimeProbe:
    def __init__(
        self,
        websocket_url: str,
        *,
        requester: Requester | None = None,
    ) -> None:
        parsed = urlsplit(websocket_url)
        scheme = "https" if parsed.scheme == "wss" else "http"
        self.url = urlunsplit((scheme, parsed.netloc, "/probe", "", ""))
        self.requester = requester or _default_request

    async def __call__(self) -> ProbeEvidence:
        try:
            payload = _json_object(await self.requester("POST", self.url))
        except (httpx.HTTPError, ValueError, TypeError):
            return self._failure("FASTER_WHISPER_UNAVAILABLE")
        pid = payload.get("worker_pid")
        if (
            payload.get("ready") is not True
            or payload.get("loaded") is not True
            or payload.get("status") not in {"ready", "busy"}
            or not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or payload.get("model") != "medium"
            or payload.get("device") != "cuda"
            or payload.get("compute_type") != "int8_float16"
        ):
            return self._failure("FASTER_WHISPER_IDENTITY_MISMATCH")
        return ProbeEvidence(
            name="faster_whisper",
            worker_pid=pid,
            ok=True,
            reason_code="READY",
            model="medium",
            device="cuda",
            compute_type="int8_float16",
        )

    @staticmethod
    def _failure(reason_code: str) -> ProbeEvidence:
        return ProbeEvidence("faster_whisper", 0, False, reason_code)
