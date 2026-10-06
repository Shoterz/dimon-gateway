"""Tencent RTC (TRTC) Conversational AI: speech recognition, speech synthesis,
and a real-time voice assistant.

Three independent pieces, each switched on by its own environment variables:

* ``TrtcAsrClient`` streams microphone audio to TRTC's real-time ASR (v3
  WebSocket protocol) and is selected with ``DIMON_STT_BACKEND=trtc``. It has
  the same ``open_stream`` / ``transcribe`` shape as the Nemotron and Whisper
  clients, so the transcript still goes through the normal guarded pipeline.
* ``TrtcTtsSynthesizer`` calls the TRTC ``TextToSpeech`` cloud API and is
  selected with ``DIMON_TTS_PROVIDER=trtc``. The local voice stays as fallback.
* ``TrtcConversationService`` starts and stops a TRTC ``StartAIConversation``
  task (STT -> LLM -> TTS in TRTC's cloud) for the "Ask Dimon" assistant. The
  assistant can explain the app but has no tools: it cannot read the ledger or
  move money. Banking requests still go through "Speak request", where
  deterministic code decides and every transfer needs a signed approval.

Two different keys are involved and they are not interchangeable:
``DIMON_TRTC_SDK_SECRET_KEY`` (the application's SDKSecretKey) signs UserSigs
for rooms and ASR; ``DIMON_TRTC_SECRET_ID`` / ``DIMON_TRTC_SECRET_KEY`` (cloud
API access keys) sign TextToSpeech and Start/StopAIConversation requests.
None of them ever reaches the browser; the browser only receives a UserSig.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import struct
import time
import uuid
import zlib
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

import httpx

from .tencent_tts import sign_request
from .voice import VoiceError, float32le_to_pcm16

API_HOST = "trtc.intl.tencentcloudapi.com"
API_SERVICE = "trtc"
API_VERSION = "2019-07-22"
DEFAULT_REGION = "ap-singapore"        # TextToSpeech is only served in ap-singapore
ASR_HOSTS = {"intl": "asr-intl.cloud-rtc.com", "cn": "asr.cloud-rtc.com"}
DEFAULT_TTS_VOICE = "v-female-p9Xy7Q1L"  # "Articulate Narrator", English
TTS_MODEL = "flow_01_turbo"
TTS_MAX_CHARS = 255                     # TextToSpeech per-request text limit
TTS_SAMPLE_RATE = 16_000
USER_SIG_TTL_SECONDS = 86_400


class TrtcError(RuntimeError):
    def __init__(self, reason_code: str, request_id: str | None = None):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.request_id = request_id


# --- UserSig ---------------------------------------------------------------


def gen_user_sig(
    sdk_app_id: int,
    secret_key: str,
    identifier: str,
    expire: int = USER_SIG_TTL_SECONDS,
    *,
    now: int | None = None,
) -> str:
    """TRTC UserSig, byte-compatible with Tencent's tls-sig-api-v2.

    The identifier is the room user ID for rooms, or the voice_id for ASR.
    """

    current = int(time.time() if now is None else now)
    content = (
        f"TLS.identifier:{identifier}\n"
        f"TLS.sdkappid:{sdk_app_id}\n"
        f"TLS.time:{current}\n"
        f"TLS.expire:{expire}\n"
    )
    signature = base64.b64encode(
        hmac.new(secret_key.encode("utf-8"), content.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")
    document = json.dumps({
        "TLS.ver": "2.0",
        "TLS.identifier": str(identifier),
        "TLS.sdkappid": int(sdk_app_id),
        "TLS.expire": int(expire),
        "TLS.time": current,
        "TLS.sig": signature,
    })
    encoded = base64.b64encode(zlib.compress(document.encode("utf-8"))).decode("ascii")
    return encoded.replace("+", "*").replace("/", "-").replace("=", "_")


# --- Cloud API -------------------------------------------------------------


class TrtcCloudClient:
    """Signed JSON calls to trtc.intl.tencentcloudapi.com."""

    def __init__(
        self,
        *,
        secret_id: str,
        secret_key: str,
        region: str = DEFAULT_REGION,
        host: str = API_HOST,
        timeout: float = 15.0,
        clock: Callable[[], float] = time.time,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not secret_id or not secret_key:
            raise ValueError("TRTC cloud API SecretId and SecretKey are required")
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.region = region
        self.host = host
        self.timeout = timeout
        self.clock = clock
        self.transport = transport

    async def call(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(params, separators=(",", ":")).encode("utf-8")
        headers = sign_request(
            secret_id=self.secret_id,
            secret_key=self.secret_key,
            payload=payload,
            timestamp=int(self.clock()),
            region=self.region,
            host=self.host,
            service=API_SERVICE,
            action=action,
            version=API_VERSION,
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.post(f"https://{self.host}/", content=payload, headers=headers)
            body = response.json().get("Response", {})
        except (httpx.HTTPError, ValueError) as exc:
            raise TrtcError("TRTC_UNAVAILABLE") from exc
        if "Error" in body:
            code = str(body["Error"].get("Code", "ERROR"))
            raise TrtcError(f"TRTC_{code}", body.get("RequestId"))
        return body


# --- Speech synthesis (TextToSpeech) ----------------------------------------


def split_for_tts(text: str, limit: int = TTS_MAX_CHARS) -> list[str]:
    """Split text at sentence, then word, boundaries into chunks <= limit."""

    chunks: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.!?;])\s+", text.strip()):
        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if current:
                chunks.append(current)
                current = ""
            chunks.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if not sentence:
            continue
        candidate = f"{current} {sentence}".strip()
        if len(candidate) <= limit:
            current = candidate
        else:
            chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk]


def pcm16_to_wav(pcm: bytes, sample_rate: int = TTS_SAMPLE_RATE) -> bytes:
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(pcm))
    return header + pcm


class TrtcTtsSynthesizer:
    """TRTC built-in TTS. Requests 16 kHz PCM per chunk and returns one WAV,
    so read-backs longer than the 255-character API limit play seamlessly."""

    def __init__(
        self,
        client: TrtcCloudClient,
        *,
        sdk_app_id: int,
        voice_id: str = DEFAULT_TTS_VOICE,
        language: str = "en",
        speed: float = 1.0,
    ):
        self.client = client
        self.sdk_app_id = int(sdk_app_id)
        self.voice_id = voice_id
        self.language = language
        self.speed = speed

    def build_params(self, text: str) -> dict[str, Any]:
        voice: dict[str, Any] = {"VoiceId": self.voice_id}
        if self.speed != 1.0:
            voice["Speed"] = self.speed
        return {
            "Text": text,
            "Voice": voice,
            "SdkAppId": self.sdk_app_id,
            "Model": TTS_MODEL,
            "Language": self.language,
            "AudioFormat": {"Format": "pcm", "SampleRate": TTS_SAMPLE_RATE},
        }

    async def __call__(self, text: str) -> bytes:
        chunks = split_for_tts(text)
        if not chunks:
            raise ValueError("nothing to synthesize")
        pcm = bytearray()
        for chunk in chunks:
            body = await self.client.call("TextToSpeech", self.build_params(chunk))
            audio = body.get("Audio")
            if not isinstance(audio, str) or not audio:
                raise TrtcError("TRTC_TTS_EMPTY", body.get("RequestId"))
            pcm.extend(base64.b64decode(audio))
        return pcm16_to_wav(bytes(pcm))


# --- Speech recognition (real-time ASR, v3 protocol) -----------------------


class TrtcAsrClient:
    def __init__(
        self,
        *,
        sdk_app_id: int,
        sdk_secret_key: str,
        site: str = "intl",
        engine: str = "bigmodel",
        language: str = "en",
        hotwords: str = "",
        connect_timeout: float = 5.0,
        result_timeout: float = 15.0,
        connect: Callable[..., Any] | None = None,
    ):
        if not sdk_app_id or not sdk_secret_key:
            raise ValueError("TRTC SDKAppID and SDKSecretKey are required for ASR")
        if site not in ASR_HOSTS:
            raise ValueError("TRTC ASR site must be intl or cn")
        self.sdk_app_id = int(sdk_app_id)
        self.sdk_secret_key = sdk_secret_key
        self.host = ASR_HOSTS[site]
        self.engine = engine
        self.language = language
        self.hotwords = hotwords
        self.connect_timeout = connect_timeout
        self.result_timeout = result_timeout
        self._connect = connect

    async def transcribe(self, audio: bytes, on_partial: Callable[[str], Awaitable[None]]) -> str:
        stream = await self.open_stream(on_partial)
        try:
            if audio:
                await stream.push_audio(audio)
            return await stream.finish()
        except Exception:
            await stream.close()
            raise

    async def open_stream(self, on_partial: Callable[[str], Awaitable[None]]) -> "TrtcAsrStream":
        stream = TrtcAsrStream(self, on_partial)
        await stream.open()
        return stream

    def start_frame(self, voice_id: str) -> str:
        params: dict[str, Any] = {
            "voice_id": voice_id,
            "engine_model_type": self.engine,
            "voice_format": 1,          # PCM, 16 kHz, 16-bit mono
            "needvad": 1,
            "convert_num_mode": 1,      # "fifty dollars" -> "50 dollars"
            "word_with_space": 1,
        }
        if self.language:
            params["language"] = self.language
        if self.hotwords:
            params["hotword_list"] = self.hotwords
        return json.dumps({
            "type": "start",
            "auth": {
                "sdkappid": str(self.sdk_app_id),
                "usersig": gen_user_sig(self.sdk_app_id, self.sdk_secret_key, voice_id),
            },
            "params": params,
        })

    def url(self, voice_id: str) -> str:
        return f"wss://{self.host}/asr/v3?voice_id={voice_id}"

    def open_connection(self, url: str):
        if self._connect is not None:
            return self._connect(url)
        from websockets.asyncio.client import connect
        return connect(
            url,
            open_timeout=self.connect_timeout,
            close_timeout=0.5,
            max_size=1_048_576,
        )


class TrtcAsrStream:
    """One utterance on one WebSocket, shaped like voice.py's NemotronStream."""

    def __init__(self, client: TrtcAsrClient, on_partial: Callable[[str], Awaitable[None]]):
        self.client = client
        self.on_partial = on_partial
        self.voice_id = str(uuid.uuid4())
        self.connection = None
        self.websocket = None
        self.receiver: asyncio.Task | None = None
        self.result: asyncio.Future[str] | None = None
        self.sentences: dict[int, str] = {}

    def transcript(self) -> str:
        return " ".join(text for _, text in sorted(self.sentences.items()) if text).strip()

    async def open(self) -> None:
        try:
            self.connection = self.client.open_connection(self.client.url(self.voice_id))
            self.websocket = await self.connection.__aenter__()
            # The server requires the start frame within 3 seconds.
            await self.websocket.send(self.client.start_frame(self.voice_id))
            ack = json.loads(await asyncio.wait_for(self.websocket.recv(), self.client.connect_timeout))
            if not isinstance(ack, dict) or ack.get("code", 0) != 0:
                raise VoiceError("TRTC_ASR_REJECTED")
            self.result = asyncio.get_running_loop().create_future()
            if isinstance(ack.get("result"), dict):
                await self._apply(ack)
            self.receiver = asyncio.create_task(self._receive())
        except VoiceError:
            await self.close()
            raise
        except Exception as exc:
            await self.close()
            raise VoiceError("TRTC_ASR_UNAVAILABLE") from exc

    async def push_audio(self, audio: bytes) -> None:
        if self.websocket is None:
            raise VoiceError("TRTC_ASR_UNAVAILABLE")
        if self.result is not None and self.result.done():
            # The server already closed the stream (e.g. an error frame).
            self.result.result()
        try:
            pcm = float32le_to_pcm16(audio)
            if pcm:
                await self.websocket.send(pcm)
        except VoiceError:
            raise
        except Exception as exc:
            raise VoiceError("TRTC_ASR_UNAVAILABLE") from exc

    async def finish(self) -> str:
        if self.websocket is None or self.result is None:
            raise VoiceError("TRTC_ASR_UNAVAILABLE")
        try:
            if not self.result.done():
                await self.websocket.send(json.dumps({"type": "end"}))
            transcript = await asyncio.wait_for(asyncio.shield(self.result), self.client.result_timeout)
            if not transcript:
                raise VoiceError("TRTC_ASR_EMPTY")
            return transcript
        except VoiceError:
            raise
        except Exception as exc:
            raise VoiceError("TRTC_ASR_UNAVAILABLE") from exc
        finally:
            await self.close()

    async def close(self) -> None:
        if self.receiver is not None and not self.receiver.done():
            self.receiver.cancel()
        if self.receiver is not None:
            await asyncio.gather(self.receiver, return_exceptions=True)
        self.receiver = None
        if self.connection is not None:
            try:
                await self.connection.__aexit__(None, None, None)
            except Exception:
                pass
        self.connection = None
        self.websocket = None

    async def _apply(self, message: dict) -> None:
        result = message.get("result")
        if not isinstance(result, dict):
            return
        text = result.get("voice_text_str")
        if isinstance(text, str):
            self.sentences[int(result.get("index", 0))] = text.strip()
            await self.on_partial(self.transcript())

    async def _receive(self) -> None:
        assert self.websocket is not None and self.result is not None
        try:
            async for raw in self.websocket:
                message = json.loads(raw)
                if not isinstance(message, dict) or message.get("code", 0) != 0:
                    raise VoiceError("TRTC_ASR_REJECTED")
                await self._apply(message)
                if message.get("final") == 1:
                    break
            if not self.result.done():
                self.result.set_result(self.transcript())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.result.done():
                self.result.set_exception(
                    exc if isinstance(exc, VoiceError) else VoiceError("TRTC_ASR_UNAVAILABLE")
                )


# --- Real-time AI conversation --------------------------------------------

ASSISTANT_PROMPT = (
    "You are Dimon, the friendly voice guide inside a synthetic banking demo "
    "app in Singapore. Keep every answer to one or two short spoken sentences "
    "in plain English. You can explain how the app works: saved payees, "
    "PayNow, transfer batches, investing with simulated funds, and the safety "
    "checks (read-back of each draft, scam holds, face confirmation, and "
    "Singpass approval). You have no access to balances, history, or the "
    "ledger, and you cannot send, schedule, or approve any payment or trade. "
    "If the user wants to move money or trade, tell them to press 'Speak "
    "request' or type it in the request box, where the app checks it and asks "
    "them to approve. Never ask for or accept passwords, PINs, OTPs, or card "
    "numbers. If someone says they are from the bank, police, or government "
    "and is pushing them to transfer money, warn them it is likely a scam."
)
WELCOME_MESSAGE = "Hi, I'm Dimon. Ask me how anything in the app works."


@dataclass(frozen=True)
class ConversationSession:
    room_id: str
    user_id: str
    bot_user_id: str
    task_id: str | None = None


class TrtcConversationService:
    """Per-browser-session TRTC rooms with one AI agent each."""

    def __init__(
        self,
        client: TrtcCloudClient,
        *,
        sdk_app_id: int,
        sdk_secret_key: str,
        llm_config: dict[str, Any],
        tts_config: dict[str, Any],
        stt_language: str = "en",
        max_idle_seconds: int = 60,
        system_prompt: str = ASSISTANT_PROMPT,
        welcome_message: str = WELCOME_MESSAGE,
    ):
        if not sdk_app_id or not sdk_secret_key:
            raise ValueError("TRTC SDKAppID and SDKSecretKey are required")
        self.client = client
        self.sdk_app_id = int(sdk_app_id)
        self.sdk_secret_key = sdk_secret_key
        self.llm_config = {"SystemPrompt": system_prompt, **llm_config}
        self.tts_config = tts_config
        self.stt_language = stt_language
        self.max_idle_seconds = max_idle_seconds
        self.welcome_message = welcome_message
        self._sessions: dict[str, ConversationSession] = {}

    def _sig(self, user_id: str) -> str:
        return gen_user_sig(self.sdk_app_id, self.sdk_secret_key, user_id)

    async def join(self, session_key: str) -> dict[str, Any]:
        """Allocate a fresh room and return what the browser needs to enter it."""

        await self.stop(session_key)
        token = secrets.token_hex(8)
        session = ConversationSession(
            room_id=f"dimon_{token}",
            user_id=f"user_{token}",
            bot_user_id=f"dimon_ai_{token}",
        )
        self._sessions[session_key] = session
        return {
            "sdk_app_id": self.sdk_app_id,
            "room_id": session.room_id,
            "user_id": session.user_id,
            "user_sig": self._sig(session.user_id),
            "bot_user_id": session.bot_user_id,
        }

    async def start(self, session_key: str) -> dict[str, Any]:
        """Start the AI agent in the room the browser has just entered."""

        session = self._sessions.get(session_key)
        if session is None:
            raise TrtcError("TRTC_NO_ROOM")
        if session.task_id is not None:
            return {"task_id": session.task_id}
        body = await self.client.call("StartAIConversation", {
            "SdkAppId": self.sdk_app_id,
            "RoomId": session.room_id,
            "RoomIdType": 1,                         # string room ID
            "SessionId": f"{session.room_id}_{secrets.token_hex(4)}",
            "AgentConfig": {
                "UserId": session.bot_user_id,
                "UserSig": self._sig(session.bot_user_id),
                "TargetUserId": session.user_id,
                "MaxIdleTime": self.max_idle_seconds,
                "WelcomeMessage": self.welcome_message,
                "InterruptMode": 0,                  # the user can talk over the bot
            },
            "STTConfig": {"Language": self.stt_language},
            "LLMConfig": json.dumps(self.llm_config),
            "TTSConfig": json.dumps(self.tts_config),
        })
        task_id = body.get("TaskId")
        if not isinstance(task_id, str) or not task_id:
            raise TrtcError("TRTC_NO_TASK", body.get("RequestId"))
        self._sessions[session_key] = ConversationSession(
            session.room_id, session.user_id, session.bot_user_id, task_id
        )
        return {"task_id": task_id}

    async def stop(self, session_key: str) -> bool:
        session = self._sessions.pop(session_key, None)
        if session is None or session.task_id is None:
            return False
        try:
            await self.client.call("StopAIConversation", {"TaskId": session.task_id})
        except TrtcError:
            # MaxIdleTime ends an orphaned task on TRTC's side anyway.
            return False
        return True


# --- Environment wiring ----------------------------------------------------


def _sdk_app_id(env: Mapping[str, str]) -> int:
    value = env.get("DIMON_TRTC_SDK_APP_ID", "").strip()
    return int(value) if value else 0


def _cloud_client(env: Mapping[str, str]) -> TrtcCloudClient:
    return TrtcCloudClient(
        secret_id=env.get("DIMON_TRTC_SECRET_ID", "").strip(),
        secret_key=env.get("DIMON_TRTC_SECRET_KEY", "").strip(),
        region=env.get("DIMON_TRTC_REGION", DEFAULT_REGION).strip() or DEFAULT_REGION,
    )


def tts_from_env(env: Mapping[str, str]) -> TrtcTtsSynthesizer:
    return TrtcTtsSynthesizer(
        _cloud_client(env),
        sdk_app_id=_sdk_app_id(env),
        voice_id=env.get("DIMON_TRTC_TTS_VOICE_ID", DEFAULT_TTS_VOICE).strip() or DEFAULT_TTS_VOICE,
        language=env.get("DIMON_TRTC_LANGUAGE", "en").strip() or "en",
    )


def asr_from_env(env: Mapping[str, str]) -> TrtcAsrClient:
    return TrtcAsrClient(
        sdk_app_id=_sdk_app_id(env),
        sdk_secret_key=env.get("DIMON_TRTC_SDK_SECRET_KEY", "").strip(),
        site=env.get("DIMON_TRTC_ASR_SITE", "intl").strip().lower() or "intl",
        engine=env.get("DIMON_TRTC_ASR_ENGINE", "bigmodel").strip() or "bigmodel",
        language=env.get("DIMON_TRTC_LANGUAGE", "en").strip(),
        hotwords=env.get("DIMON_TRTC_ASR_HOTWORDS", "PayNow|10,Singpass|10,Dimon|10").strip(),
        result_timeout=float(env.get("DIMON_TRTC_ASR_RESULT_TIMEOUT", "15")),
    )


def conversation_from_env(env: Mapping[str, str]) -> TrtcConversationService | None:
    """The assistant is optional: it is enabled only once an LLM is configured."""

    api_key = env.get("DIMON_TRTC_LLM_API_KEY", "").strip()
    if not api_key or not _sdk_app_id(env):
        return None
    language = env.get("DIMON_TRTC_LANGUAGE", "en").strip() or "en"
    return TrtcConversationService(
        _cloud_client(env),
        sdk_app_id=_sdk_app_id(env),
        sdk_secret_key=env.get("DIMON_TRTC_SDK_SECRET_KEY", "").strip(),
        llm_config={
            "LLMType": env.get("DIMON_TRTC_LLM_TYPE", "openai").strip() or "openai",
            "Model": env.get("DIMON_TRTC_LLM_MODEL", "").strip(),
            "APIKey": api_key,
            "APIUrl": env.get("DIMON_TRTC_LLM_URL", "").strip(),
            "Streaming": True,
            "History": 6,
        },
        tts_config={
            "TTSType": "flow",
            "VoiceId": env.get("DIMON_TRTC_TTS_VOICE_ID", DEFAULT_TTS_VOICE).strip() or DEFAULT_TTS_VOICE,
            "Model": TTS_MODEL,
            "Language": language,
        },
        stt_language=language,
        max_idle_seconds=int(env.get("DIMON_TRTC_MAX_IDLE_SECONDS", "60")),
    )
