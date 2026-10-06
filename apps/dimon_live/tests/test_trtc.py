import asyncio
import base64
import hashlib
import hmac
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

import httpx

from apps.dimon_live import trtc
from apps.dimon_live.inference import ProviderConfigurationError
from apps.dimon_live.provider_factory import build_inference_providers
from apps.dimon_live.tencent_tts import FallbackSynthesizer, read_back_synthesizer_from_env
from apps.dimon_live.voice import VoiceError

CLOCK = lambda: 1_700_000_000  # noqa: E731


def decode_user_sig(sig: str) -> dict:
    raw = sig.replace("*", "+").replace("-", "/").replace("_", "=")
    return json.loads(zlib.decompress(base64.b64decode(raw)))


def float_audio(samples: int = 160) -> bytes:
    return struct.pack(f"<{samples}f", *([0.25] * samples))


class UserSigTests(unittest.TestCase):
    def test_user_sig_matches_tls_sig_api_v2_format(self):
        sig = trtc.gen_user_sig(1400000001, "sdk-secret", "user_1", 600, now=1_700_000_000)
        document = decode_user_sig(sig)
        self.assertEqual(document["TLS.ver"], "2.0")
        self.assertEqual(document["TLS.identifier"], "user_1")
        self.assertEqual(document["TLS.sdkappid"], 1400000001)
        self.assertEqual(document["TLS.expire"], 600)
        self.assertEqual(document["TLS.time"], 1_700_000_000)
        content = "TLS.identifier:user_1\nTLS.sdkappid:1400000001\nTLS.time:1700000000\nTLS.expire:600\n"
        expected = base64.b64encode(hmac.new(b"sdk-secret", content.encode(), hashlib.sha256).digest()).decode()
        self.assertEqual(document["TLS.sig"], expected)
        self.assertNotRegex(sig, r"[+/=]")


class CloudApiTests(unittest.TestCase):
    def client(self, handler):
        return trtc.TrtcCloudClient(
            secret_id="AKIDtest", secret_key="cloud-secret",
            transport=httpx.MockTransport(handler), clock=CLOCK,
        )

    def test_calls_are_signed_for_the_trtc_service(self):
        seen = {}

        def handler(request):
            seen["headers"] = request.headers
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"Response": {"RequestId": "r1"}})

        asyncio.run(self.client(handler).call("StopAIConversation", {"TaskId": "t"}))
        self.assertEqual(seen["url"], "https://trtc.intl.tencentcloudapi.com/")
        self.assertEqual(seen["headers"]["X-TC-Action"], "StopAIConversation")
        self.assertEqual(seen["headers"]["X-TC-Version"], "2019-07-22")
        self.assertEqual(seen["headers"]["X-TC-Region"], "ap-singapore")
        self.assertIn("Credential=AKIDtest/2023-11-14/trtc/tc3_request", seen["headers"]["Authorization"])

    def test_api_errors_become_reason_codes(self):
        def handler(request):
            return httpx.Response(200, json={"Response": {"Error": {"Code": "AuthFailure.SignatureFailure"}, "RequestId": "r2"}})

        with self.assertRaises(trtc.TrtcError) as raised:
            asyncio.run(self.client(handler).call("TextToSpeech", {}))
        self.assertEqual(raised.exception.reason_code, "TRTC_AuthFailure.SignatureFailure")
        self.assertEqual(raised.exception.request_id, "r2")


class TextToSpeechTests(unittest.TestCase):
    def test_split_keeps_every_chunk_under_the_api_limit(self):
        text = "Send fifty dollars to Alicia Powell. " * 20 + "x" * 300
        chunks = trtc.split_for_tts(text)
        self.assertTrue(all(len(chunk) <= trtc.TTS_MAX_CHARS for chunk in chunks))
        self.assertEqual("".join(chunks).replace(" ", ""), text.replace(" ", ""))
        self.assertEqual(trtc.split_for_tts("Short one."), ["Short one."])

    def test_synthesizer_joins_pcm_chunks_into_one_wav(self):
        bodies = []

        def handler(request):
            body = json.loads(request.content)
            bodies.append(body)
            pcm = b"\x01\x00" * len(body["Text"])
            return httpx.Response(200, json={"Response": {"Audio": base64.b64encode(pcm).decode(), "RequestId": "r"}})

        client = trtc.TrtcCloudClient(secret_id="i", secret_key="k", transport=httpx.MockTransport(handler), clock=CLOCK)
        tts = trtc.TrtcTtsSynthesizer(client, sdk_app_id=1400000001)
        text = "A" * 200 + ". " + "B" * 200 + "."
        wav = asyncio.run(tts(text))

        self.assertEqual(len(bodies), 2)
        self.assertEqual(bodies[0]["SdkAppId"], 1400000001)
        self.assertEqual(bodies[0]["Voice"], {"VoiceId": trtc.DEFAULT_TTS_VOICE})
        self.assertEqual(bodies[0]["AudioFormat"], {"Format": "pcm", "SampleRate": 16000})
        self.assertEqual(wav[:4], b"RIFF")
        self.assertEqual(wav[8:12], b"WAVE")
        data_length = struct.unpack("<I", wav[40:44])[0]
        self.assertEqual(data_length, 2 * sum(len(b["Text"]) for b in bodies))
        self.assertEqual(len(wav), 44 + data_length)

    def test_tts_provider_trtc_wraps_the_local_voice_as_fallback(self):
        async def local(text):
            return b"local"

        synth = read_back_synthesizer_from_env({
            "DIMON_TTS_PROVIDER": "trtc",
            "DIMON_TRTC_SDK_APP_ID": "1400000001",
            "DIMON_TRTC_SECRET_ID": "id",
            "DIMON_TRTC_SECRET_KEY": "key",
        }, local)
        self.assertIsInstance(synth, FallbackSynthesizer)
        self.assertIsInstance(synth.preferred, trtc.TrtcTtsSynthesizer)


class FakeAsrSocket:
    def __init__(self, replies, ack=None):
        self.sent = []
        self.ack = ack or {"code": 0, "message": "success"}
        self.replies = replies
        self.ended = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, message):
        self.sent.append(message)
        if isinstance(message, str) and json.loads(message).get("type") == "end":
            self.ended.set()

    async def recv(self):
        return json.dumps(self.ack)

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        await self.ended.wait()
        for reply in self.replies:
            yield json.dumps(reply)


class SpeechRecognitionTests(unittest.TestCase):
    def asr(self, socket):
        self.urls = []

        def connect(url):
            self.urls.append(url)
            return socket

        return trtc.TrtcAsrClient(sdk_app_id=1400000001, sdk_secret_key="sdk-secret", connect=connect, result_timeout=2)

    def test_stream_authenticates_and_returns_the_final_sentences(self):
        socket = FakeAsrSocket([
            {"code": 0, "result": {"slice_type": 1, "index": 0, "voice_text_str": "Send John"}},
            {"code": 0, "result": {"slice_type": 2, "index": 0, "voice_text_str": "Send John Smith 50 dollars."}},
            {"code": 0, "result": {"slice_type": 2, "index": 1, "voice_text_str": "From savings."}},
            {"code": 0, "final": 1},
        ])
        partials = []

        async def on_partial(text):
            partials.append(text)

        transcript = asyncio.run(self.asr(socket).transcribe(float_audio(), on_partial))

        self.assertEqual(transcript, "Send John Smith 50 dollars. From savings.")
        self.assertEqual(partials[0], "Send John")
        start = json.loads(socket.sent[0])
        self.assertEqual(start["type"], "start")
        self.assertEqual(start["auth"]["sdkappid"], "1400000001")
        voice_id = start["params"]["voice_id"]
        self.assertEqual(self.urls, [f"wss://asr-intl.cloud-rtc.com/asr/v3?voice_id={voice_id}"])
        self.assertEqual(decode_user_sig(start["auth"]["usersig"])["TLS.identifier"], voice_id)
        self.assertEqual(start["params"]["engine_model_type"], "bigmodel")
        self.assertEqual(start["params"]["language"], "en")
        # Browser float32 audio is converted to 16-bit PCM before sending.
        self.assertIsInstance(socket.sent[1], bytes)
        self.assertEqual(len(socket.sent[1]), 160 * 2)
        self.assertEqual(json.loads(socket.sent[-1]), {"type": "end"})

    def test_rejected_authentication_is_a_voice_error(self):
        socket = FakeAsrSocket([], ack={"code": 4002, "message": "auth failed"})

        async def on_partial(text):
            pass

        with self.assertRaises(VoiceError) as raised:
            asyncio.run(self.asr(socket).open_stream(on_partial))
        self.assertEqual(raised.exception.reason_code, "TRTC_ASR_REJECTED")

    def test_silence_is_reported_as_empty(self):
        socket = FakeAsrSocket([{"code": 0, "final": 1}])

        async def on_partial(text):
            pass

        with self.assertRaises(VoiceError) as raised:
            asyncio.run(self.asr(socket).transcribe(float_audio(), on_partial))
        self.assertEqual(raised.exception.reason_code, "TRTC_ASR_EMPTY")

    def test_stt_backend_trtc_replaces_the_recognizer_in_any_mode(self):
        env = {"DIMON_STT_BACKEND": "trtc", "DIMON_TRTC_SDK_APP_ID": "1400000001", "DIMON_TRTC_SDK_SECRET_KEY": "s"}
        for mode in ("fixture", "local"):
            with self.subTest(mode=mode):
                providers = build_inference_providers(mode, env)
                self.assertIsInstance(providers.recognizer, trtc.TrtcAsrClient)
                self.assertIsNone(providers.voice_lifecycle)
        with self.assertRaises(ProviderConfigurationError) as raised:
            build_inference_providers("fixture", {"DIMON_STT_BACKEND": "trtc"})
        self.assertEqual(raised.exception.reason_code, "INVALID_TRTC_CONFIG")


class ConversationTests(unittest.TestCase):
    def service(self, calls, task_id="task-1"):
        def handler(request):
            body = json.loads(request.content)
            calls.append((request.headers["X-TC-Action"], body))
            if request.headers["X-TC-Action"] == "StartAIConversation":
                return httpx.Response(200, json={"Response": {"TaskId": task_id, "RequestId": "r"}})
            return httpx.Response(200, json={"Response": {"RequestId": "r"}})

        client = trtc.TrtcCloudClient(secret_id="i", secret_key="k", transport=httpx.MockTransport(handler), clock=CLOCK)
        return trtc.TrtcConversationService(
            client,
            sdk_app_id=1400000001,
            sdk_secret_key="sdk-secret",
            llm_config={"LLMType": "openai", "Model": "m", "APIKey": "llm-key", "APIUrl": "https://llm.test/v1/chat/completions"},
            tts_config={"TTSType": "flow", "VoiceId": "v"},
        )

    def test_join_start_stop_lifecycle(self):
        calls = []
        service = self.service(calls)
        room = asyncio.run(service.join("visitor-1"))

        self.assertEqual(decode_user_sig(room["user_sig"])["TLS.identifier"], room["user_id"])
        self.assertNotEqual(room["user_id"], room["bot_user_id"])
        self.assertEqual(calls, [])

        self.assertEqual(asyncio.run(service.start("visitor-1")), {"task_id": "task-1"})
        action, body = calls[0]
        self.assertEqual(action, "StartAIConversation")
        self.assertEqual(body["RoomId"], room["room_id"])
        self.assertEqual(body["RoomIdType"], 1)
        self.assertEqual(body["AgentConfig"]["UserId"], room["bot_user_id"])
        self.assertEqual(body["AgentConfig"]["TargetUserId"], room["user_id"])
        self.assertEqual(decode_user_sig(body["AgentConfig"]["UserSig"])["TLS.identifier"], room["bot_user_id"])
        llm = json.loads(body["LLMConfig"])
        self.assertIn("cannot send", llm["SystemPrompt"])
        self.assertEqual(llm["APIKey"], "llm-key")
        self.assertEqual(json.loads(body["TTSConfig"])["TTSType"], "flow")

        # Starting twice reuses the running task.
        asyncio.run(service.start("visitor-1"))
        self.assertEqual(len(calls), 1)

        self.assertTrue(asyncio.run(service.stop("visitor-1")))
        self.assertEqual(calls[-1], ("StopAIConversation", {"TaskId": "task-1"}))
        self.assertFalse(asyncio.run(service.stop("visitor-1")))

    def test_sessions_are_isolated_and_rejoin_stops_the_old_task(self):
        calls = []
        service = self.service(calls)
        asyncio.run(service.join("a"))
        with self.assertRaises(trtc.TrtcError):
            asyncio.run(service.start("b"))
        asyncio.run(service.start("a"))
        asyncio.run(service.join("a"))
        self.assertEqual(calls[-1][0], "StopAIConversation")

    def test_assistant_is_disabled_until_an_llm_is_configured(self):
        self.assertIsNone(trtc.conversation_from_env({"DIMON_TRTC_SDK_APP_ID": "1400000001"}))
        service = trtc.conversation_from_env({
            "DIMON_TRTC_SDK_APP_ID": "1400000001",
            "DIMON_TRTC_SDK_SECRET_KEY": "s",
            "DIMON_TRTC_SECRET_ID": "i",
            "DIMON_TRTC_SECRET_KEY": "k",
            "DIMON_TRTC_LLM_API_KEY": "llm",
            "DIMON_TRTC_LLM_MODEL": "m",
            "DIMON_TRTC_LLM_URL": "https://llm.test/v1/chat/completions",
        })
        self.assertIsInstance(service, trtc.TrtcConversationService)


class FakeAssistant:
    def __init__(self):
        self.calls = []

    async def join(self, key):
        self.calls.append(("join", key))
        return {"sdk_app_id": 1, "room_id": "r", "user_id": "u", "user_sig": "sig", "bot_user_id": "b"}

    async def start(self, key):
        self.calls.append(("start", key))
        raise trtc.TrtcError("TRTC_InvalidParameter", "req-9")

    async def stop(self, key):
        self.calls.append(("stop", key))
        return True


class AssistantApiTests(unittest.TestCase):
    def setUp(self):
        from apps.dimon_live.app import create_app

        self.temp_dir = tempfile.TemporaryDirectory()
        self.assistant = FakeAssistant()
        self.create_app = create_app
        self.app = create_app(Path(self.temp_dir.name) / "demo.sqlite3", trtc_assistant=self.assistant)

    def tearDown(self):
        self.temp_dir.cleanup()

    def request(self, app, method, path):
        async def send():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                return await client.request(method, path)

        return asyncio.run(send())

    def test_routes_drive_the_assistant_and_surface_trtc_errors(self):
        self.assertTrue(self.request(self.app, "GET", "/api/trtc").json()["assistant"])
        joined = self.request(self.app, "POST", "/api/trtc/assistant/join")
        self.assertEqual(joined.status_code, 200)
        self.assertEqual(joined.json()["user_sig"], "sig")
        started = self.request(self.app, "POST", "/api/trtc/assistant/start")
        self.assertEqual(started.status_code, 502)
        self.assertEqual(started.json()["detail"], "TRTC_InvalidParameter")
        self.assertEqual(self.request(self.app, "POST", "/api/trtc/assistant/stop").json(), {"stopped": True})
        self.assertEqual([call[0] for call in self.assistant.calls], ["join", "start", "stop"])

    def test_disabled_assistant_returns_503(self):
        app = self.create_app(Path(self.temp_dir.name) / "other.sqlite3")
        self.assertFalse(self.request(app, "GET", "/api/trtc").json()["assistant"])
        self.assertEqual(self.request(app, "POST", "/api/trtc/assistant/join").status_code, 503)

    def test_assistant_script_is_served(self):
        response = self.request(self.app, "GET", "/static/trtc_assistant.mjs")
        self.assertEqual(response.status_code, 200)
        self.assertIn("initTrtcAssistant", response.text)


if __name__ == "__main__":
    unittest.main()
