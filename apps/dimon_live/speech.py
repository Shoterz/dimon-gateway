"""Kokoro result speech with session-scoped ephemeral artifacts."""

from __future__ import annotations

import json
import secrets
import time
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlparse

import httpx


@dataclass(frozen=True)
class SpeechArtifact:
    artifact_id: str
    mime_type: str = "audio/wav"


def build_result_summary(
    *,
    status: str,
    action_kind: str,
    amount: int,
    currency: str,
    recipient_display: str | None,
    reference: str | None = None,
    batch_count: int | None = None,
) -> str | None:
    """Construct the only result text that local or gateway TTS may receive."""

    if currency != "SGD" or not isinstance(amount, int) or amount <= 0:
        return None
    amount_text = f"{amount // 100} dollars and {amount % 100:02d} cents"
    if status == "EXECUTED_SYNTHETIC_TRANSFER" and action_kind in {
        "BENEFICIARY_TRANSFER",
        "PAYNOW_MOBILE_TRANSFER",
    }:
        if not recipient_display:
            return None
        summary = f"Synthetic transfer complete. {amount_text} moved to {recipient_display}."
    elif (
        status == "EXECUTED_SYNTHETIC_BATCH"
        and action_kind == "BENEFICIARY_TRANSFER_BATCH"
    ):
        if batch_count is not None:
            summary = (
                f"Synthetic batch complete. {amount_text} moved across "
                f"{batch_count} transfers."
            )
        elif recipient_display:
            summary = f"Synthetic batch complete. {amount_text} moved to {recipient_display}."
        else:
            return None
    else:
        return None
    if reference:
        summary += f" Reference {reference}."
    return summary


class KokoroSynthesizer:
    def __init__(self, base_url: str, *, timeout: float = 30.0):
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Kokoro URL must be HTTP")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def __call__(self, text: str) -> bytes:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout, trust_env=False) as client:
            response = await client.post(
                "/gradio_api/call/KOKORO_TTS_API",
                json={"data": [text, "American English", "af_heart", 1.15, False, False, "WAV", 300, 150]},
            )
            response.raise_for_status()
            event_id = response.json().get("event_id")
            if not isinstance(event_id, str):
                raise RuntimeError("Kokoro did not return an event ID")
            audio_url = None
            async with client.stream("GET", f"/gradio_api/call/KOKORO_TTS_API/{event_id}") as stream:
                stream.raise_for_status()
                event_type = ""
                async for line in stream.aiter_lines():
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:") and event_type == "complete":
                        payload = json.loads(line[5:].strip())
                        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                            audio_url = payload[0].get("url")
                            break
                    elif line.startswith("data:") and event_type == "error":
                        raise RuntimeError("Kokoro synthesis failed")
            if not isinstance(audio_url, str):
                raise RuntimeError("Kokoro queue ended without audio")
            resolved = urljoin(self.base_url + "/", audio_url)
            if urlparse(resolved).netloc != urlparse(self.base_url).netloc:
                raise RuntimeError("Kokoro returned an untrusted audio URL")
            audio = await client.get(resolved)
            audio.raise_for_status()
            return audio.content


class SpeechService:
    def __init__(
        self,
        *,
        base_url: str,
        audio_dir: Path,
        synthesizer: Callable[[str], Awaitable[bytes]] | None = None,
        max_wav_bytes: int = 8 * 1_048_576,
        ttl_seconds: int = 3600,
    ):
        self.base_url = base_url
        self.audio_dir = Path(audio_dir)
        self.synthesizer = synthesizer or KokoroSynthesizer(base_url)
        self.max_wav_bytes = int(max_wav_bytes)
        self.ttl_seconds = int(ttl_seconds)
        self._artifacts: dict[str, tuple[str, Path, float]] = {}
        self._pending: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    @staticmethod
    def _summary(result: dict) -> str | None:
        if result.get("outcome") == "EXECUTED_SYNTHETIC_TRANSFER":
            action = result.get("resolved_action") or {}
            recipient = action.get("recipient_display_name")
            amount = result.get("amount_minor")
            if not isinstance(recipient, str) or not isinstance(amount, int):
                return None
            return build_result_summary(
                status="EXECUTED_SYNTHETIC_TRANSFER",
                action_kind="BENEFICIARY_TRANSFER",
                amount=amount,
                currency="SGD",
                recipient_display=recipient,
            )
        if result.get("outcome") == "EXECUTED_SYNTHETIC_BATCH":
            amount = result.get("amount_minor")
            count = len(result.get("resolved_actions") or [])
            if not isinstance(amount, int):
                return None
            return build_result_summary(
                status="EXECUTED_SYNTHETIC_BATCH",
                action_kind="BENEFICIARY_TRANSFER_BATCH",
                amount=amount,
                currency="SGD",
                recipient_display=None,
                batch_count=count,
            )
        return None

    async def speak_result(self, result: dict, context) -> SpeechArtifact | None:
        text = self._summary(result)
        if text is None:
            return None
        artifact_id = secrets.token_urlsafe(18)
        path = self.audio_dir / context.session_id / f"{artifact_id}.wav"
        self._artifacts[artifact_id] = (context.session_id, path, time.time() + self.ttl_seconds)
        try:
            await self._synthesize_into(text, path)
            return SpeechArtifact(artifact_id) if path.is_file() else None
        except Exception:
            self._artifacts.pop(artifact_id, None)
            return None

    def queue_result(self, result: dict, context) -> str | None:
        """Reserve an audio artifact and synthesize it outside the response path."""
        text = self._summary(result)
        if text is None:
            return None
        artifact_id = secrets.token_urlsafe(18)
        path = self.audio_dir / context.session_id / f"{artifact_id}.wav"
        self._artifacts[artifact_id] = (context.session_id, path, time.time() + self.ttl_seconds)
        self._pending.add(artifact_id)
        task = asyncio.create_task(self._synthesize_queued(text, artifact_id, path))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return artifact_id

    async def _synthesize_queued(self, text: str, artifact_id: str, path: Path) -> None:
        try:
            await self._synthesize_into(text, path)
            if not path.is_file():
                self._artifacts.pop(artifact_id, None)
        except Exception:
            self._artifacts.pop(artifact_id, None)
        finally:
            self._pending.discard(artifact_id)

    async def _synthesize_into(self, text: str, path: Path) -> None:
        data = await self.synthesizer(text)
        if not isinstance(data, bytes) or len(data) > self.max_wav_bytes:
            return
        if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def is_pending(self, artifact_id: str, context) -> bool:
        record = self._artifacts.get(artifact_id)
        return bool(record and record[0] == context.session_id and artifact_id in self._pending)

    async def read(self, artifact_id: str, context) -> bytes | None:
        self.cleanup()
        record = self._artifacts.get(artifact_id)
        if record is None or record[0] != context.session_id:
            return None
        try:
            return record[1].read_bytes()
        except OSError:
            return None

    def cleanup(self) -> None:
        now = time.time()
        expired = [key for key, (_, path, expiry) in self._artifacts.items() if expiry <= now]
        for key in expired:
            _, path, _ = self._artifacts.pop(key)
            self._pending.discard(key)
            path.unlink(missing_ok=True)
