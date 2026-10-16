"""Tencent Cloud Text-To-Speech (TextToVoice) for draft read-backs.

Enabled with DIMON_TTS_PROVIDER=tencent plus DIMON_TENCENT_SECRET_ID and
DIMON_TENCENT_SECRET_KEY (optionally DIMON_TENCENT_REGION and
DIMON_TENCENT_TTS_VOICE_TYPE). Requests are signed with Tencent Cloud's
TC3-HMAC-SHA256 scheme. Only the read-back sentence is sent: amounts, payee
names, and account endings the user is about to approve.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Awaitable, Callable, Mapping

import httpx

HOST = "tts.tencentcloudapi.com"
SERVICE = "tts"
ACTION = "TextToVoice"
VERSION = "2019-08-23"
DEFAULT_VOICE_TYPE = 101051   # English female voice
MAX_TEXT_CHARS = 500


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def sign_request(
    *,
    secret_id: str,
    secret_key: str,
    payload: bytes,
    timestamp: int,
    region: str,
    host: str = HOST,
    service: str = SERVICE,
    action: str = ACTION,
    version: str = VERSION,
) -> dict[str, str]:
    """Headers for a TC3-HMAC-SHA256 signed Tencent Cloud API call.

    The defaults sign a TextToVoice call; trtc.py reuses this for TRTC APIs.
    """

    date = time.strftime("%Y-%m-%d", time.gmtime(timestamp))
    content_type = "application/json; charset=utf-8"
    canonical_headers = f"content-type:{content_type}\nhost:{host}\nx-tc-action:{action.lower()}\n"
    signed_headers = "content-type;host;x-tc-action"
    canonical_request = "\n".join(
        ("POST", "/", "", canonical_headers, signed_headers, _sha256_hex(payload))
    )
    scope = f"{date}/{service}/tc3_request"
    string_to_sign = "\n".join(
        ("TC3-HMAC-SHA256", str(timestamp), scope, _sha256_hex(canonical_request.encode("utf-8")))
    )
    signing_key = _hmac(_hmac(_hmac(f"TC3{secret_key}".encode("utf-8"), date), service), "tc3_request")
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "Authorization": (
            f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
        "Content-Type": content_type,
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": version,
        "X-TC-Region": region,
    }


class TencentTtsSynthesizer:
    def __init__(
        self,
        *,
        secret_id: str,
        secret_key: str,
        region: str = "ap-singapore",
        voice_type: int = DEFAULT_VOICE_TYPE,
        timeout: float = 15.0,
        clock: Callable[[], float] = time.time,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not secret_id or not secret_key:
            raise ValueError("Tencent Cloud credentials are required")
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.region = region
        self.voice_type = voice_type
        self.timeout = timeout
        self.clock = clock
        self.transport = transport

    def build_payload(self, text: str) -> bytes:
        return json.dumps(
            {
                "Text": text[:MAX_TEXT_CHARS],
                "SessionId": str(uuid.uuid4()),
                "VoiceType": self.voice_type,
                "PrimaryLanguage": 2,       # English
                "Codec": "wav",
                "SampleRate": 16000,
            },
            separators=(",", ":"),
        ).encode("utf-8")

    async def __call__(self, text: str) -> bytes:
        payload = self.build_payload(text)
        headers = sign_request(
            secret_id=self.secret_id,
            secret_key=self.secret_key,
            payload=payload,
            timestamp=int(self.clock()),
            region=self.region,
        )
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            response = await client.post(f"https://{HOST}/", content=payload, headers=headers)
        response.raise_for_status()
        body = response.json().get("Response", {})
        if "Error" in body:
            raise RuntimeError(f"TENCENT_TTS_{body['Error'].get('Code', 'ERROR')}")
        return base64.b64decode(body["Audio"])


class FallbackSynthesizer:
    """Try the preferred voice; if it fails, use the local one."""

    def __init__(self, preferred: Callable[[str], Awaitable[bytes]], fallback: Callable[[str], Awaitable[bytes]]):
        self.preferred = preferred
        self.fallback = fallback

    async def __call__(self, text: str) -> bytes:
        try:
            return await self.preferred(text)
        except Exception:
            return await self.fallback(text)


def read_back_synthesizer_from_env(env: Mapping[str, str], default):
    del env
    # Provider selection used to make Tencent or TRTC primary. Runtime speech
    # now owns one order: configured Kokoro/gateway first, TRTC only on an
    # eligible outage. Keep this compatibility helper order-preserving.
    return default
