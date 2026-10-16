"""The hands-free socket: wake word, follow-ups, spoken confirmation."""

import os
import asyncio
import json
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from apps.dimon_live.hands_free import HandsFreeBridge, Segmenter, WakePartialMatcher, enqueue_latest_utterance
from apps.dimon_live.voice import VoiceError, VoiceLease
from apps.dimon_live.voice_trace import VoiceTraceStore
from apps.dimon_live.voiceprint import ENROLL_PROMPTS, VoiceRejected, digits_heard
from apps.dimon_live.tests.voice_fake import clip


_ENV_BEFORE = None


def setUpModule():
    # These tests cover the optional second "everyday" account.
    global _ENV_BEFORE
    _ENV_BEFORE = os.environ.get("DIMON_EVERYDAY_ACCOUNT")
    os.environ["DIMON_EVERYDAY_ACCOUNT"] = "1"


def tearDownModule():
    if _ENV_BEFORE is None:
        os.environ.pop("DIMON_EVERYDAY_ACCOUNT", None)
    else:
        os.environ["DIMON_EVERYDAY_ACCOUNT"] = _ENV_BEFORE

CHUNK = 4096


class WakePartialMatcherTests(unittest.TestCase):
    def test_partial_matcher_accepts_exact_standalone_wake_immediately(self):
        self.assertEqual(WakePartialMatcher().push("Hey Dimon"), (True, ""))

    def test_partial_matcher_accepts_bounded_standalone_variants(self):
        variant = WakePartialMatcher()
        self.assertEqual(variant.push("Hey Diamond"), (True, ""))
        self.assertEqual(variant.push("Hey Diamond"), (False, ""))
        self.assertEqual(WakePartialMatcher().push("Hi Diamon"), (True, ""))

    def test_partial_matcher_accepts_forward_progress_instead_of_identical_text(self):
        command = WakePartialMatcher()
        self.assertEqual(command.push("Hey Diamond send"), (False, ""))
        self.assertEqual(command.push("Hey Diamond send Alicia"), (True, "send Alicia"))

    def test_partial_matcher_final_can_confirm_one_command_candidate(self):
        matcher = WakePartialMatcher()
        self.assertEqual(matcher.push("Hey Dimon send Alicia"), (False, ""))
        self.assertEqual(matcher.push("Hey Dimon send Alicia", final=True), (True, "send Alicia"))

    def test_partial_matcher_resets_transient_or_non_wake_matches(self):
        matcher = WakePartialMatcher()
        self.assertEqual(matcher.push("Hey Diamond send"), (False, ""))
        self.assertEqual(matcher.push("the demon in the film"), (False, ""))
        self.assertEqual(matcher.push("Hey Diamond send"), (False, ""))
        self.assertEqual(matcher.push("Hey Diamond send Alicia"), (True, "send Alicia"))

    def test_partial_matcher_rejects_incomplete_incidental_and_demon_phrases(self):
        for text in ("Hey", "Diamond", "please say Hey Dimon", "Hey Demon", "the demon in the film"):
            with self.subTest(text=text):
                self.assertEqual(WakePartialMatcher().push(text), (False, ""))


class ScriptedRecognizer:
    """Returns the next scripted transcript for each utterance."""

    def __init__(self, transcripts):
        self.transcripts = list(transcripts)

    async def transcribe(self, audio, on_partial):
        return self.transcripts.pop(0) if self.transcripts else ""


class PartialRecognizer(ScriptedRecognizer):
    async def transcribe(self, audio, on_partial):
        await on_partial("last for")
        await on_partial("last for today")
        return await super().transcribe(audio, on_partial)


class CountingRecognizer(ScriptedRecognizer):
    def __init__(self, transcripts):
        super().__init__(transcripts)
        self.calls = 0

    async def transcribe(self, audio, on_partial):
        self.calls += 1
        return await super().transcribe(audio, on_partial)


class PartialThenFailRecognizer:
    async def transcribe(self, _audio, on_partial):
        await on_partial("Hey Dimon")
        raise VoiceError("NEMOTRON_UNAVAILABLE", "transcription_finish")


class SlowRecognizer(ScriptedRecognizer):
    """Takes longer per utterance than the bridge would wait for a busy model."""

    async def transcribe(self, audio, on_partial):
        await asyncio.sleep(0.4)
        return await super().transcribe(audio, on_partial)


class RecordingLiveStream:
    def __init__(self, final="authoritative final"):
        self.final = final
        self.audio = []
        self.closed = False

    async def push_audio(self, audio):
        self.audio.append(audio)

    async def finish(self):
        return self.final

    async def close(self):
        self.closed = True


class GatedLiveRecognizer:
    def __init__(self, *, gated=False):
        self.gate = asyncio.Event()
        if not gated:
            self.gate.set()
        self.opened = asyncio.Event()
        self.stream = RecordingLiveStream()

    async def open_stream(self, _on_partial):
        self.opened.set()
        await self.gate.wait()
        return self.stream


class LiveTranscriptionSessionTests(unittest.IsolatedAsyncioTestCase):
    def make_session(self, *args, **kwargs):
        try:
            from apps.dimon_live.live_transcription import LiveTranscriptionSession
        except ModuleNotFoundError:
            self.fail("LiveTranscriptionSession is not implemented")
        return LiveTranscriptionSession(*args, **kwargs)

    async def test_live_session_opens_at_speech_start(self):
        recognizer = GatedLiveRecognizer()
        lease = VoiceLease()
        session = self.make_session(
            recognizer, lease, lambda _text: asyncio.sleep(0),
            acquire_timeout=0.2, max_pending_bytes=64,
        )

        await session.start()
        await asyncio.wait_for(recognizer.opened.wait(), timeout=0.2)
        await session.cancel()

        self.assertTrue(recognizer.stream.closed)
        self.assertEqual(lease.active_count, 0)

    async def test_live_session_queues_chunks_in_order_while_opening(self):
        recognizer = GatedLiveRecognizer(gated=True)
        session = self.make_session(
            recognizer, VoiceLease(), lambda _text: asyncio.sleep(0),
            acquire_timeout=0.2, max_pending_bytes=64,
        )

        await session.start()
        await recognizer.opened.wait()
        await session.push(b"first")
        await session.push(b"second")
        recognizer.gate.set()
        result = await session.finish()

        self.assertEqual(result, "authoritative final")
        self.assertEqual(recognizer.stream.audio, [b"first", b"second"])

    async def test_live_session_never_duplicates_start_chunk(self):
        recognizer = GatedLiveRecognizer()
        session = self.make_session(
            recognizer, VoiceLease(), lambda _text: asyncio.sleep(0),
            acquire_timeout=0.2, max_pending_bytes=64,
        )

        await session.start()
        await session.push(b"pre-roll-and-start")
        await session.push(b"next")
        await session.finish()

        self.assertEqual(recognizer.stream.audio, [b"pre-roll-and-start", b"next"])

    async def test_live_session_bounds_pending_audio(self):
        recognizer = GatedLiveRecognizer(gated=True)
        session = self.make_session(
            recognizer, VoiceLease(), lambda _text: asyncio.sleep(0),
            acquire_timeout=0.2, max_pending_bytes=4,
        )

        await session.start()
        await recognizer.opened.wait()
        await session.push(b"1234")
        with self.assertRaisesRegex(Exception, "AUDIO_LIMIT_EXCEEDED"):
            await session.push(b"5")
        await session.cancel()

    async def test_live_session_releases_lease_on_finish_and_cancel(self):
        finished_recognizer = GatedLiveRecognizer()
        finished_lease = VoiceLease()
        finished = self.make_session(
            finished_recognizer, finished_lease, lambda _text: asyncio.sleep(0),
            acquire_timeout=0.2, max_pending_bytes=64,
        )
        await finished.start()
        await finished.finish()

        cancelled_recognizer = GatedLiveRecognizer(gated=True)
        cancelled_lease = VoiceLease()
        cancelled = self.make_session(
            cancelled_recognizer, cancelled_lease, lambda _text: asyncio.sleep(0),
            acquire_timeout=0.2, max_pending_bytes=64,
        )
        await cancelled.start()
        await cancelled_recognizer.opened.wait()
        await cancelled.cancel()

        self.assertEqual(finished_lease.active_count, 0)
        self.assertEqual(cancelled_lease.active_count, 0)
        self.assertTrue(finished_recognizer.stream.closed)


class BlockingWakeRecognizer:
    def __init__(self, final="Hey Dimon"):
        self.final = final
        self.release = threading.Event()

    async def transcribe(self, audio, on_partial):
        await on_partial("Hey Dimon")
        await on_partial("Hey Dimon")
        await asyncio.to_thread(self.release.wait, 2)
        return self.final


class FakeLedger:
    def __init__(self, *, registered=False, similarity=0.9):
        self.registered = registered
        self.similarity_by_speaker = similarity
        self.verified = []
        self.voice_store = SimpleNamespace(speaker_similarity=self._similarity)
        self.next_speaker_similarity = similarity
        self.locked = False
        self.enrolled = []

    def lock_status(self):
        return {"locked": self.locked, "locked_at": None}

    def voice_status(self):
        return {"voice_registered": self.registered, "voice_max_minor": 20_000}

    def _similarity(self, user_id, audio):
        return self.next_speaker_similarity

    def verify_voice(self, challenge_id, audio, transcript, prior_request_audio=None):
        self.verified.append((challenge_id, transcript))
        if "4719" not in digits_heard(transcript):
            raise VoiceRejected("VOICE_PHRASE_MISMATCH")
        return {"sample_id": "voice_1", "verdict": "MATCH", "similarity": 0.91}

    def add_voice_enrollment_sample(self, enrollment_id, audio):
        self.enrolled.append(enrollment_id)
        return {"samples": 1, "samples_needed": 3, "complete": False}


class ProgressiveStreamingRecognizer:
    def __init__(self, partials, final, *, finish_partials=()):
        self.partials = list(partials)
        self.finish_partials = list(finish_partials)
        self.final = final
        self.stream = None

    async def open_stream(self, on_partial):
        owner = self

        class Stream:
            def __init__(self):
                self.audio = []
                self.closed = False

            async def push_audio(self, audio):
                self.audio.append(audio)
                if owner.partials:
                    await on_partial(owner.partials.pop(0))

            async def finish(self):
                for partial in owner.finish_partials:
                    await on_partial(partial)
                return owner.final

            async def close(self):
                self.closed = True

        self.stream = Stream()
        return self.stream


class HangingFinalStreamingRecognizer:
    """Emits a confident wake partial, then never produces a final result."""

    def __init__(self):
        self.stream = None

    async def open_stream(self, on_partial):
        class Stream:
            def __init__(self):
                self.partial_sent = False
                self.closed = False

            async def push_audio(stream, _audio):
                if not stream.partial_sent:
                    stream.partial_sent = True
                    await on_partial("Hey Dimon")

            async def finish(stream):
                await asyncio.Event().wait()

            async def close(stream):
                stream.closed = True

        self.stream = Stream()
        return self.stream


class DirectBridgeSocket:
    def __init__(self, chunks, terminal_types, *, initial_messages=(), final_messages=()):
        self.messages = [
            *initial_messages,
            *({"bytes": chunk} for chunk in chunks),
            *final_messages,
        ]
        self.terminal_types = set(terminal_types)
        self.sent = []
        self.terminal = asyncio.Event()

    async def accept(self):
        return None

    async def send_json(self, message):
        self.sent.append(message)
        if message.get("type") in self.terminal_types:
            self.terminal.set()

    async def receive(self):
        if self.messages:
            return self.messages.pop(0)
        await asyncio.wait_for(self.terminal.wait(), timeout=2)
        # Let the utterance worker finish any authoritative full-audio speaker
        # check before simulating the browser disconnect.
        await asyncio.sleep(0.05)
        return {"type": "websocket.disconnect"}

    async def close(self, code=1000):
        return None


def run_direct_bridge(bridge, socket, ledger):
    async def cooperative_to_thread(function, *args, **kwargs):
        await asyncio.sleep(0)
        return function(*args, **kwargs)

    with patch("asyncio.to_thread", new=cooperative_to_thread):
        asyncio.run(bridge.handle(
            socket, SimpleNamespace(database=ledger), lambda _text, _evidence: {},
        ))


def run_direct_live_wake(*, partials, final, ledger, terminal_types, speaker_timeout=0.1,
                         finish_partials=(), audio_seconds=0.7):
    recognizer = ProgressiveStreamingRecognizer(partials, final, finish_partials=finish_partials)
    dispatched = []
    bridge = HandsFreeBridge(
        recognizer=ScriptedRecognizer([]), lease=VoiceLease(),
        fast_wake_recognizer=recognizer, fast_wake_lease=VoiceLease(),
        wake_speaker_timeout=speaker_timeout,
    )
    audio = clip(seconds=audio_seconds, silence_after=1.0)
    chunks = [audio[offset:offset + CHUNK] for offset in range(0, len(audio), CHUNK)]
    socket = DirectBridgeSocket(chunks, terminal_types)

    async def dispatch(text, evidence):
        dispatched.append((text, evidence))
        return {"outcome": "ANSWERED", "answer": text}

    async def cooperative_to_thread(function, *args, **kwargs):
        # This test environment's Python 3.12 executor does not terminate at
        # asyncio.run() shutdown. A small cooperative delay still exercises
        # the timeout/arbitration behavior without leaking executor threads.
        await asyncio.sleep(0.01)
        return function(*args, **kwargs)

    with patch("asyncio.to_thread", new=cooperative_to_thread):
        asyncio.run(bridge.handle(socket, SimpleNamespace(database=ledger), dispatch))
    return socket.sent, dispatched, recognizer


def build(
    transcripts,
    ledger=None,
    *,
    recognizer=None,
    transcribe_wait=30.0,
    wake_recognizer=None,
    fast_wake_recognizer=None,
    dispatch_result=None,
    trace_store=None,
):
    ledger = ledger or FakeLedger()
    dispatched = []
    bridge = HandsFreeBridge(recognizer=recognizer or ScriptedRecognizer(transcripts), lease=VoiceLease(),
                             transcribe_wait=transcribe_wait, wake_recognizer=wake_recognizer,
                             wake_lease=VoiceLease() if wake_recognizer else None,
                             fast_wake_recognizer=fast_wake_recognizer,
                             fast_wake_lease=VoiceLease() if fast_wake_recognizer else None,
                             trace_store=trace_store)
    app = FastAPI()

    @app.websocket("/ws")
    async def socket(websocket: WebSocket):
        async def dispatch(text, evidence):
            dispatched.append((text, evidence))
            return dispatch_result or {"outcome": "ANSWERED", "answer": f"did {text}"}

        await bridge.handle(websocket, SimpleNamespace(database=ledger), dispatch)

    return TestClient(app), dispatched, ledger


def speak(ws, speaker="account-holder"):
    audio = clip(speaker, 1.6, silence_after=1.2)
    for offset in range(0, len(audio), CHUNK):
        ws.send_bytes(audio[offset:offset + CHUNK])


def receive_until(ws, kind):
    seen = []
    while True:
        message = ws.receive_json()
        seen.append(message)
        if message["type"] == kind:
            return message, seen


class SegmenterTests(unittest.TestCase):
    def test_started_segment_exposes_pre_roll_for_live_session(self):
        segmenter = Segmenter()
        speech = np.full(int(16_000 * 0.3), 0.2, dtype="<f4").tobytes()
        chunk_bytes = int(16_000 * 0.1) * 4
        for offset in range(0, len(speech), chunk_bytes):
            event, _ = segmenter.push(speech[offset:offset + chunk_bytes])
            if event == "started":
                break

        self.assertEqual(event, "started")
        self.assertGreaterEqual(len(segmenter.active_audio()), chunk_bytes)

    def test_wake_only_segment_can_be_capped_at_two_and_a_half_seconds(self):
        segmenter = Segmenter()
        segmenter.set_max_seconds(2.5)
        speech = np.full(int(16_000 * 3.0), 0.2, dtype="<f4").tobytes()
        chunk_bytes = int(16_000 * 0.1) * 4
        utterance = None
        for offset in range(0, len(speech), chunk_bytes):
            event, audio = segmenter.push(speech[offset:offset + chunk_bytes])
            if event == "utterance":
                utterance = audio
                break

        self.assertIsNotNone(utterance)
        self.assertLessEqual(len(utterance) // 4, int(16_000 * 2.6))
    def test_default_wake_utterance_finishes_within_eight_tenths_of_silence(self):
        segmenter = Segmenter()
        speech = np.full(int(16_000 * 0.3), 0.2, dtype="<f4").tobytes()
        silence = np.zeros(int(16_000 * 1.0), dtype="<f4").tobytes()
        chunk_bytes = int(16_000 * 0.1) * 4

        for offset in range(0, len(speech), chunk_bytes):
            segmenter.push(speech[offset:offset + chunk_bytes])

        silence_chunks = 0
        for offset in range(0, len(silence), chunk_bytes):
            silence_chunks += 1
            event, _utterance = segmenter.push(silence[offset:offset + chunk_bytes])
            if event == "utterance":
                break

        self.assertLessEqual(silence_chunks, 8)

    def test_one_utterance_is_cut_after_silence(self):
        segmenter = Segmenter()
        audio = clip(silence_after=1.2)
        events = []
        for offset in range(0, len(audio), CHUNK):
            event, utterance = segmenter.push(audio[offset:offset + CHUNK])
            if event:
                events.append((event, len(utterance) if utterance else 0))
        self.assertEqual([e for e, _ in events], ["started", "utterance"])
        self.assertGreater(events[1][1] // 4, 16_000)

    def test_an_utterance_ends_over_steady_background_noise(self):
        # A fan or a TV louder than the fixed threshold used to keep the
        # utterance open until the 15-second cap.
        rng = np.random.default_rng(3)
        hum = lambda seconds: (0.03 * rng.standard_normal(int(16_000 * seconds))).astype("<f4")
        speech = np.frombuffer(clip(seconds=1.6), dtype="<f4") + hum(1.6)
        audio = np.concatenate([hum(3.0), speech, hum(1.5)]).astype("<f4").tobytes()
        segmenter = Segmenter()
        events = []
        for offset in range(0, len(audio), CHUNK):
            event, utterance = segmenter.push(audio[offset:offset + CHUNK])
            if event:
                events.append((event, len(utterance) // 4 if utterance else 0))
        self.assertEqual([e for e, _ in events], ["started", "utterance"])
        self.assertLess(events[1][1], 16_000 * 4)

    def test_silence_alone_is_never_an_utterance(self):
        segmenter = Segmenter()
        silence = bytes(CHUNK * 40)
        for offset in range(0, len(silence), CHUNK):
            self.assertEqual(segmenter.push(silence[offset:offset + CHUNK]), (None, None))


class HandsFreeTests(unittest.TestCase):
    def test_progressive_wake_extends_capture_for_long_one_breath_command(self):
        _sent, dispatched, _ = run_direct_live_wake(
            partials=["Hey Dimon send", "Hey Dimon send Alicia"],
            final="Hey Dimon send Alicia twenty five dollars from savings",
            ledger=FakeLedger(), terminal_types={"action_result"}, audio_seconds=3.0,
        )

        self.assertEqual(len(dispatched), 1)
        self.assertGreater(dispatched[0][1]["audio_seconds"], 2.5)

    def test_partial_bare_wake_completes_only_after_authoritative_final(self):
        sent, _dispatched, _ = run_direct_live_wake(
            partials=["Hey Dimon"], final="Hey Dimon", ledger=FakeLedger(),
            terminal_types={"wake_complete"},
        )

        self.assertEqual(
            [item["type"] for item in sent if item["type"] in {"wake", "wake_complete"}],
            ["wake", "wake_complete"],
        )
        self.assertEqual(
            next(item for item in sent if item["type"] == "wake")["path"],
            "nemotron_partial",
        )

    def test_confident_partial_has_bounded_completion_when_finalizer_hangs(self):
        recognizer = HangingFinalStreamingRecognizer()
        fast_lease = VoiceLease()
        bridge = HandsFreeBridge(
            recognizer=ScriptedRecognizer([]),
            lease=VoiceLease(),
            fast_wake_recognizer=recognizer,
            fast_wake_lease=fast_lease,
            wake_finalize_timeout=0.02,
        )
        audio = clip(seconds=0.7, silence_after=1.0)
        socket = DirectBridgeSocket(
            [audio[offset:offset + CHUNK] for offset in range(0, len(audio), CHUNK)],
            {"wake_complete"},
        )

        async def cooperative_to_thread(function, *args, **kwargs):
            await asyncio.sleep(0)
            return function(*args, **kwargs)

        with patch("asyncio.to_thread", new=cooperative_to_thread):
            asyncio.run(bridge.handle(
                socket,
                SimpleNamespace(database=FakeLedger()),
                lambda _text, _evidence: {"outcome": "ANSWERED"},
            ))

        self.assertEqual(
            [item["type"] for item in socket.sent if item["type"] in {
                "wake", "wake_complete", "error",
            }],
            ["wake", "wake_complete"],
        )
        completion = next(item for item in socket.sent if item["type"] == "wake_complete")
        self.assertEqual(completion["path"], "nemotron_partial")
        self.assertTrue(completion["degraded_final"])
        self.assertEqual(fast_lease.active_count, 0)
        self.assertTrue(recognizer.stream.closed)

    def test_confident_non_streaming_partial_does_not_start_fallback_after_final_failure(self):
        fallback = CountingRecognizer([])
        fast_lease = VoiceLease()
        bridge = HandsFreeBridge(
            recognizer=fallback,
            lease=VoiceLease(),
            fast_wake_recognizer=PartialThenFailRecognizer(),
            fast_wake_lease=fast_lease,
        )
        audio = clip(seconds=0.7, silence_after=1.0)
        socket = DirectBridgeSocket(
            [audio[offset:offset + CHUNK] for offset in range(0, len(audio), CHUNK)],
            {"wake_complete", "not_woken", "error"},
        )

        async def cooperative_to_thread(function, *args, **kwargs):
            await asyncio.sleep(0)
            return function(*args, **kwargs)

        with patch("asyncio.to_thread", new=cooperative_to_thread):
            asyncio.run(bridge.handle(
                socket,
                SimpleNamespace(database=FakeLedger()),
                lambda _text, _evidence: {"outcome": "ANSWERED"},
            ))

        self.assertEqual(
            [item["type"] for item in socket.sent if item["type"] in {
                "wake", "wake_complete", "not_woken", "error",
            }],
            ["wake", "wake_complete"],
        )
        self.assertEqual(fallback.calls, 0)
        self.assertEqual(fast_lease.active_count, 0)

    def test_sleep_after_early_wake_cancels_inflight_stream(self):
        recognizer = ProgressiveStreamingRecognizer(["Hey Dimon"], "Hey Dimon")
        lease = VoiceLease()
        bridge = HandsFreeBridge(
            recognizer=ScriptedRecognizer([]), lease=VoiceLease(),
            fast_wake_recognizer=recognizer, fast_wake_lease=lease,
        )
        audio = clip(seconds=0.7, silence_after=1.0)

        class SleepOnWakeSocket(DirectBridgeSocket):
            async def send_json(self, message):
                await super().send_json(message)
                if message.get("type") == "wake":
                    self.messages[:] = [{"text": json.dumps({"type": "sleep"})}]

        socket = SleepOnWakeSocket(
            [audio[offset:offset + CHUNK] for offset in range(0, len(audio), CHUNK)],
            {"wake"},
        )

        asyncio.run(bridge.handle(
            socket, SimpleNamespace(database=FakeLedger()),
            lambda _text, _evidence: {"outcome": "ANSWERED"},
        ))

        self.assertTrue(recognizer.stream.closed)
        self.assertEqual(lease.active_count, 0)
        self.assertEqual(len([item for item in socket.sent if item["type"] == "wake"]), 1)

    def test_partial_arriving_during_finish_cannot_duplicate_final_wake(self):
        sent, _dispatched, _ = run_direct_live_wake(
            partials=[], finish_partials=["Hey Dimon"], final="Hey Dimon",
            ledger=FakeLedger(), terminal_types={"wake"},
        )

        self.assertEqual([item["path"] for item in sent if item["type"] == "wake"], [
            "nemotron_partial",
        ])

    def test_unregistered_partial_wakes_without_voice_check(self):
        sent, dispatched, _ = run_direct_live_wake(
            partials=["Hey Dimon"], final="Hey Dimon", ledger=FakeLedger(),
            terminal_types={"wake"},
        )

        wake = next(message for message in sent if message["type"] == "wake")
        self.assertEqual(wake["path"], "nemotron_partial")
        self.assertNotIn("command", wake)
        self.assertEqual(dispatched, [])

    def test_partial_waits_for_registered_speaker_match(self):
        sent, _dispatched, _ = run_direct_live_wake(
            partials=["Hey Dimon"], final="Hey Dimon",
            ledger=FakeLedger(registered=True, similarity=0.9), terminal_types={"wake"},
        )

        wake = next(message for message in sent if message["type"] == "wake")
        self.assertEqual(wake["path"], "nemotron_partial")
        self.assertEqual(wake["speaker"], "MATCH")

    def test_partial_never_wakes_on_speaker_unknown_or_timeout(self):
        unknown_sent, _dispatched, _ = run_direct_live_wake(
            partials=["Hey Dimon"], final="Hey Dimon",
            ledger=FakeLedger(registered=True, similarity=None), terminal_types={"ignored"},
        )
        self.assertFalse(any(
            item.get("type") == "wake" and item.get("path") == "nemotron_partial"
            for item in unknown_sent
        ))

        slow = FakeLedger(registered=True, similarity=0.9)
        original = slow.voice_store.speaker_similarity
        slow.voice_store.speaker_similarity = lambda *args: (time.sleep(0.05), original(*args))[1]
        timeout_sent, _dispatched, _ = run_direct_live_wake(
            partials=["Hey Dimon"], final="Hey Dimon", ledger=slow,
            terminal_types={"wake"}, speaker_timeout=0.001,
        )
        self.assertFalse(any(
            item.get("type") == "wake" and item.get("path") == "nemotron_partial"
            for item in timeout_sent
        ))

    def test_partial_command_never_dispatches_before_authoritative_final(self):
        sent, dispatched, _ = run_direct_live_wake(
            partials=["Hey Diamond send", "Hey Diamond send a leisure"],
            final="Hey Dimon send Alicia two dollars", ledger=FakeLedger(),
            terminal_types={"action_result"},
        )

        wake = next(message for message in sent if message["type"] == "wake")
        self.assertNotIn("command", wake)
        self.assertEqual([text for text, _evidence in dispatched], ["send Alicia two dollars"])

    def test_live_wake_stream_receives_audio_before_utterance_endpoint(self):
        class StreamingRecognizer:
            def __init__(self):
                self.stream = None

            async def open_stream(self, on_partial):
                class Stream:
                    def __init__(self):
                        self.audio = []
                        self.partial_sent = False
                        self.closed = False

                    async def push_audio(inner_self, audio):
                        inner_self.audio.append(audio)
                        if not inner_self.partial_sent:
                            inner_self.partial_sent = True
                            await on_partial("Hey Dimon")

                    async def finish(inner_self):
                        return "Hey Dimon"

                    async def close(inner_self):
                        inner_self.closed = True

                self.stream = Stream()
                return self.stream

        class Socket:
            def __init__(self, chunks):
                self.messages = [{"bytes": chunk} for chunk in chunks]
                self.sent = []
                self.woke = asyncio.Event()

            async def accept(self):
                return None

            async def send_json(self, message):
                self.sent.append(message)
                if message.get("type") == "wake":
                    self.woke.set()

            async def receive(self):
                if self.messages:
                    return self.messages.pop(0)
                await asyncio.wait_for(self.woke.wait(), timeout=1)
                return {"type": "websocket.disconnect"}

            async def close(self, code=1000):
                return None

        fast = StreamingRecognizer()
        bridge = HandsFreeBridge(
            recognizer=ScriptedRecognizer([]), lease=VoiceLease(),
            fast_wake_recognizer=fast, fast_wake_lease=VoiceLease(),
        )
        audio = clip(seconds=0.5, silence_after=1.0)
        chunks = [audio[offset:offset + CHUNK] for offset in range(0, len(audio), CHUNK)]
        socket = Socket(chunks)
        context = SimpleNamespace(database=FakeLedger())

        asyncio.run(bridge.handle(socket, context, lambda _text, _evidence: {}))

        types = [message["type"] for message in socket.sent]
        self.assertIn("wake", types)
        self.assertLess(types.index("wake"), types.index("processing"))
        self.assertIsNotNone(fast.stream)
        self.assertGreater(len(fast.stream.audio), 0)

    def test_lock_invalidates_pending_approval_and_enrollment_modes(self):
        cases = (
            ("approval", {"challenge_id": "chal_test"}, "four seven one nine"),
            ("enroll", {"enrollment_id": "enroll_test"}, ENROLL_PROMPTS[0]),
        )
        for expect, extra, transcript in cases:
            with self.subTest(expect=expect):
                ledger = FakeLedger(registered=True)
                client, _dispatched, _ = build([transcript], ledger=ledger)
                with client.websocket_connect("/ws") as ws:
                    ws.receive_json()
                    ws.send_json({"type": "listen", "expect": expect, **extra})
                    receive_until(ws, "listening")
                    ledger.locked = True
                    speak(ws)
                    error, _seen = receive_until(ws, "error")
                self.assertEqual(error["reason_code"], "APP_LOCKED")
                self.assertEqual(ledger.verified, [])
                self.assertEqual(ledger.enrolled, [])

    def test_lock_detected_during_dispatch_invalidates_the_socket(self):
        client, _dispatched, _ = build(
            ["Hey Dimon, what is my balance"],
            dispatch_result={"outcome": "BLOCKED", "reason_code": "APP_LOCKED"},
        )
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            speak(ws)
            error, _seen = receive_until(ws, "error")
        self.assertEqual(error["reason_code"], "APP_LOCKED")

    def test_speech_without_the_wake_word_is_dropped_without_its_words(self):
        client, dispatched, _ = build(["send alicia two dollars"])
        with client.websocket_connect("/ws") as ws:
            self.assertEqual(ws.receive_json()["type"], "ready")
            speak(ws)
            _, seen = receive_until(ws, "not_woken")
        # Only content-free progress comes back, never what was said.
        self.assertEqual([m["type"] for m in seen], ["speech_started", "processing", "not_woken"])
        self.assertNotIn("alicia", json.dumps(seen).lower())
        self.assertEqual(dispatched, [])

    def test_non_speech_captions_are_not_words(self):
        client, dispatched, _ = build(["(hissing) [BLANK_AUDIO]"])
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"type": "listen", "expect": "confirm"})
            receive_until(ws, "listening")
            speak(ws)
            _, seen = receive_until(ws, "processing")
            ws.send_json({"type": "listen", "expect": "command"})
            receive_until(ws, "listening")
        # Background hiss is never taken as a reply or a command.
        self.assertNotIn("reply", [m["type"] for m in seen])
        self.assertEqual(dispatched, [])

    def test_active_transcriptions_wait_for_the_lease_instead_of_reporting_busy(self):
        recognizer = SlowRecognizer(["first request", "second request"])
        bridge = HandsFreeBridge(recognizer=recognizer, lease=VoiceLease(), transcribe_wait=1.0)

        async def scenario():
            return await asyncio.gather(
                bridge._transcribe_with(recognizer, bridge.lease, b"first"),
                bridge._transcribe_with(recognizer, bridge.lease, b"second"),
            )

        self.assertEqual(asyncio.run(scenario()), ["first request", "second request"])

    def test_sleeping_listener_discards_stale_chatter_before_the_latest_wake_phrase(self):
        queue = asyncio.Queue()
        queue.put_nowait((b"old room audio", {"kind": "asleep"}))
        queue.put_nowait((b"answer", {"kind": "reply"}))

        enqueue_latest_utterance(queue, b"new wake phrase", {"kind": "asleep"})

        self.assertEqual(queue.get_nowait(), (b"answer", {"kind": "reply"}))
        self.assertEqual(queue.get_nowait(), (b"new wake phrase", {"kind": "asleep"}))
        self.assertTrue(queue.empty())

    def test_sleeping_listener_returns_discarded_session_and_trace_for_cleanup(self):
        queue = asyncio.Queue()
        stale_session = object()
        stale_state = {"trace_id": str(uuid4()), "attempt_id": str(uuid4())}
        queue.put_nowait((b"old room audio", {
            "kind": "asleep", "live_session": stale_session, "live_state": stale_state,
        }))

        discarded = enqueue_latest_utterance(queue, b"new wake phrase", {"kind": "asleep"})

        self.assertEqual(discarded, [{"live_session": stale_session, "live_state": stale_state}])

    def test_cancel_steers_the_open_confirmation_and_is_never_a_request(self):
        client, dispatched, _ = build(["Hey Dimon, cancel", "Hey Dimon, cancel my monthly plan"])
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            speak(ws)
            control, _ = receive_until(ws, "control")
            speak(ws)
            receive_until(ws, "action_result")
        self.assertEqual(control["action"], "cancel")
        # Only the real request was dispatched.
        self.assertEqual([text for text, _ in dispatched], ["cancel my monthly plan"])

    def test_wake_word_with_command_dispatches_it(self):
        client, dispatched, _ = build(["Hey Dimon, send Alicia two dollars from savings"])
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            speak(ws)
            wake, _ = receive_until(ws, "wake")
            self.assertEqual(wake["command"], "send Alicia two dollars from savings")
            result, _ = receive_until(ws, "action_result")
        self.assertEqual(result["result"]["answer"], "did send Alicia two dollars from savings")
        self.assertEqual(dispatched[0][1]["hands_free"], True)

    def test_waiting_conversation_context_is_bound_to_a_later_wake_command(self):
        bridge = HandsFreeBridge(
            recognizer=ScriptedRecognizer(["Hey Dimon, buy now"]), lease=VoiceLease(),
        )
        audio = clip(seconds=0.7, silence_after=1.0)
        chunks = [audio[offset:offset + CHUNK] for offset in range(0, len(audio), CHUNK)]
        socket = DirectBridgeSocket(chunks, {"action_result"})
        socket.messages.insert(0, {"text": json.dumps({
            "type": "conversation_context",
            "conversation_id": "conversation_nvda",
            "expected_version": 2,
        })})
        dispatched = []

        async def dispatch(text, evidence):
            dispatched.append((text, evidence))
            return {"outcome": "ANSWERED", "answer": text}

        async def cooperative_to_thread(function, *args, **kwargs):
            await asyncio.sleep(0)
            return function(*args, **kwargs)

        with patch("asyncio.to_thread", new=cooperative_to_thread):
            asyncio.run(bridge.handle(
                socket, SimpleNamespace(database=FakeLedger()), dispatch,
            ))

        context = next(item for item in socket.sent if item.get("type") == "conversation_context")
        self.assertEqual(context["status"], "bound")
        self.assertEqual(dispatched[0][0], "buy now")
        self.assertEqual(dispatched[0][1]["conversation_id"], "conversation_nvda")
        self.assertEqual(dispatched[0][1]["expected_version"], 2)

    def test_fast_wake_partial_emits_before_authoritative_final_without_duplicate(self):
        fast = BlockingWakeRecognizer("Hey Dimon")
        client, _, _ = build([], fast_wake_recognizer=fast)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            speak(ws)
            wake, seen = receive_until(ws, "wake")
            self.assertEqual(wake["path"], "nemotron_partial")
            self.assertFalse(fast.release.is_set())
            fast.release.set()
            ws.send_json({"type": "stop"})
        self.assertEqual(sum(item.get("type") == "wake" for item in seen), 1)

    def test_fast_partial_command_waits_for_authoritative_final_text(self):
        class CommandFast(BlockingWakeRecognizer):
            async def transcribe(self, audio, on_partial):
                await on_partial("Hey Dimon send a leisure")
                await on_partial("Hey Dimon send a leisure payment")
                await asyncio.to_thread(self.release.wait, 2)
                return "Hey Dimon send Alicia two dollars"

        fast = CommandFast()
        client, dispatched, _ = build([], fast_wake_recognizer=fast)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            speak(ws)
            wake, _ = receive_until(ws, "wake")
            self.assertNotIn("command", wake)
            fast.release.set()
            result, _ = receive_until(ws, "action_result")
        self.assertEqual(result["text"], "send Alicia two dollars")
        self.assertEqual(dispatched[0][0], "send Alicia two dollars")

    def test_fast_wake_partial_cannot_bypass_registered_speaker(self):
        fast = BlockingWakeRecognizer("Hey Dimon")
        ledger = FakeLedger(registered=True, similarity=0.1)
        fast.release.set()
        client, dispatched, _ = build([], ledger, fast_wake_recognizer=fast)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            speak(ws, "stranger")
            ignored, seen = receive_until(ws, "ignored")
        self.assertEqual(ignored["reason_code"], "SPEAKER_MISMATCH")
        self.assertFalse(any(item.get("type") == "wake" for item in seen))
        self.assertEqual(dispatched, [])

    def test_fast_wake_partial_requires_positive_registered_speaker_match(self):
        fast = BlockingWakeRecognizer("Hey Dimon")
        ledger = FakeLedger(registered=True, similarity=None)
        fast.release.set()
        client, dispatched, _ = build([], ledger, fast_wake_recognizer=fast)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            speak(ws)
            ignored, seen = receive_until(ws, "ignored")
        self.assertEqual(ignored["reason_code"], "SPEAKER_UNVERIFIED")
        self.assertFalse(any(item.get("type") == "wake" for item in seen))
        self.assertEqual(dispatched, [])

    def test_fast_wake_failure_uses_cpu_fallback(self):
        class Broken:
            async def transcribe(self, audio, on_partial):
                raise ConnectionError("nemotron unavailable")

        local = ScriptedRecognizer(["Hey Dimon"])
        client, _, _ = build([], fast_wake_recognizer=Broken(), wake_recognizer=local)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            speak(ws)
            wake, _ = receive_until(ws, "wake")
        self.assertEqual(wake["path"], "faster_whisper_final")

    def test_trace_correlates_live_nemotron_wake_milestones_to_one_attempt(self):
        trace_store = VoiceTraceStore()
        trace_id = str(uuid4())
        fast = ProgressiveStreamingRecognizer(["Hey Dimon"], "Hey Dimon")
        audio = clip(seconds=0.7, silence_after=1.0)
        chunks = [audio[offset:offset + CHUNK] for offset in range(0, len(audio), CHUNK)]
        socket = DirectBridgeSocket(chunks, {"wake"}, initial_messages=(
            {"text": json.dumps({"type": "trace_start", "trace_id": trace_id})},
            {"text": json.dumps({
                "type": "trace_event", "trace_id": trace_id,
                "event": "d_click", "elapsed_ms": 0,
            })},
        ))
        bridge = HandsFreeBridge(
            recognizer=ScriptedRecognizer([]), lease=VoiceLease(),
            fast_wake_recognizer=fast, fast_wake_lease=VoiceLease(),
            trace_store=trace_store,
        )
        run_direct_bridge(bridge, socket, FakeLedger())

        latest = trace_store.latest()
        wake = next(item for item in socket.sent if item["type"] == "wake")
        self.assertEqual(wake["trace_id"], trace_id)
        self.assertEqual(wake["attempt_id"], latest["attempts"][0]["attempt_id"])
        self.assertEqual(latest["attempts"][0]["path"], "nemotron_partial")
        names = [event["name"] for event in latest["events"]]
        for name in (
            "d_click", "attempt_started", "speech_detected", "nemotron_stream_ready",
            "first_partial", "wake_match", "speaker_check", "wake_sent",
            "authoritative_final", "recognition_complete", "attempt_finished",
        ):
            self.assertIn(name, names)
        self.assertNotIn("transcript", repr(latest).lower())

    def test_trace_records_cuda_medium_final_and_nemotron_failure_phase(self):
        class BrokenStreamRecognizer:
            async def open_stream(self, _on_partial):
                from apps.dimon_live.voice import VoiceError
                raise VoiceError("UPSTREAM_UNAVAILABLE", "websocket_connect")

        trace_store = VoiceTraceStore()
        trace_id = str(uuid4())
        audio = clip(seconds=0.7, silence_after=1.0)
        chunks = [audio[offset:offset + CHUNK] for offset in range(0, len(audio), CHUNK)]
        socket = DirectBridgeSocket(chunks, {"wake"}, initial_messages=(
            {"text": json.dumps({"type": "trace_start", "trace_id": trace_id})},
        ))
        bridge = HandsFreeBridge(
            recognizer=ScriptedRecognizer([]), lease=VoiceLease(),
            fast_wake_recognizer=BrokenStreamRecognizer(),
            fast_wake_lease=VoiceLease(),
            wake_recognizer=ScriptedRecognizer(["Hey Dimon"]),
            wake_lease=VoiceLease(),
            trace_store=trace_store,
        )
        run_direct_bridge(bridge, socket, FakeLedger())

        attempt = trace_store.latest()["attempts"][0]
        wake = next(item for item in socket.sent if item["type"] == "wake")
        self.assertEqual(wake["path"], "faster_whisper_final")
        self.assertEqual(attempt["path"], "faster_whisper_final")
        self.assertEqual(attempt["failure_phase"], "nemotron_connect")

    def test_trace_finishes_cancelled_live_attempt_when_sleep_aborts_it(self):
        trace_store = VoiceTraceStore()
        trace_id = str(uuid4())
        fast = ProgressiveStreamingRecognizer([], "Hey Dimon")
        speech = np.full(int(16_000 * 0.3), 0.2, dtype="<f4").tobytes()
        chunks = [speech[offset:offset + CHUNK] for offset in range(0, len(speech), CHUNK)]
        socket = DirectBridgeSocket(
            chunks, set(),
            initial_messages=(
                {"text": json.dumps({"type": "trace_start", "trace_id": trace_id})},
            ),
            final_messages=(
                {"text": json.dumps({"type": "sleep"})},
                {"type": "websocket.disconnect"},
            ),
        )
        bridge = HandsFreeBridge(
            recognizer=ScriptedRecognizer([]), lease=VoiceLease(),
            fast_wake_recognizer=fast, fast_wake_lease=VoiceLease(),
            trace_store=trace_store,
        )
        run_direct_bridge(bridge, socket, FakeLedger())

        attempt = trace_store.latest()["attempts"][0]
        self.assertEqual(attempt["path"], "cancelled")
        self.assertEqual(attempt["failure_phase"], "cancelled")

    def test_browser_trace_protocol_stays_content_free_and_measures_while_hidden(self):
        source = (os.path.dirname(__file__) + "/../static/hands_free.mjs")
        with open(source, encoding="utf-8") as module:
            javascript = module.read()
        for token in (
            "trace_start", "trace_event", "d_click", "audio_ready", "microphone_ready",
            "socket_ready", "backend_ready", "browser_wake_received", "reply_audio_ready",
            "reply_playback_started", "markBackendReady", "onStarted",
        ):
            self.assertIn(token, javascript)
        self.assertNotIn("diagnosticsVisible", javascript)
        trace_bound = javascript[javascript.index('message.type === "trace_bound"'):]
        trace_bound = trace_bound[:trace_bound.index('message.type === "ready"')]
        self.assertNotIn('recordTrace("backend_ready")', trace_bound)

        with open(os.path.dirname(__file__) + "/../static/app.js", encoding="utf-8") as module:
            app_javascript = module.read()
        self.assertIn("refreshPreparation(true, traceId)", app_javascript)
        self.assertIn("markBackendReady?.(measuredTraceId)", app_javascript)

    def test_wake_telemetry_is_content_free_and_includes_path_and_latency(self):
        fast = BlockingWakeRecognizer("Hey Dimon")
        fast.release.set()
        client, _, _ = build([], fast_wake_recognizer=fast)
        with self.assertLogs("apps.dimon_live.hands_free", level="WARNING") as captured:
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()
                ws.send_json({"type": "prebuffer_flushed", "duration_ms": 640})
                speak(ws)
                receive_until(ws, "wake")
        wake_logs = [line for line in captured.output if "hands_free_wake" in line]
        self.assertTrue(wake_logs)
        self.assertIn('"wake_path":"nemotron_partial"', wake_logs[0])
        self.assertIn('"prebuffer_ms":640', wake_logs[0])
        self.assertIn('"speech_end_to_match_ms":', wake_logs[0])
        self.assertIn('"speech_end_to_wake_ms":', wake_logs[0])
        self.assertNotIn("Hey Dimon", wake_logs[0])

    def test_demon_wake_name_and_transaction_homophone_are_normalized(self):
        client, dispatched, _ = build(["Hey Demon, let's by Nvidia shares"])
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            speak(ws)
            result, _ = receive_until(ws, "action_result")

        self.assertEqual(result["text"], "let's buy Nvidia shares")
        self.assertEqual(dispatched[0][0], "let's buy Nvidia shares")
        self.assertEqual(dispatched[0][1]["transcript"], "let's by Nvidia shares")
        self.assertEqual(dispatched[0][1]["normalized_transcript"], "let's buy Nvidia shares")

    def test_wake_word_alone_then_the_command(self):
        client, dispatched, _ = build(["hey dimon", "buy one hundred dollars of nvidia"])
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            speak(ws)
            receive_until(ws, "wake")
            speak(ws)
            result, _ = receive_until(ws, "action_result")
        self.assertEqual(result["text"], "buy one hundred dollars of nvidia")

    def test_a_local_wake_recogniser_spares_the_main_one(self):
        # Chatter and the bare wake phrase never reach the main recogniser,
        # which a remote gateway allows only a few times a minute.
        main = ScriptedRecognizer(["buy one hundred dollars of nvidia"])
        local = ScriptedRecognizer(["what's for dinner", "Hydiman"])
        client, dispatched, _ = build([], recognizer=main, wake_recognizer=local)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            speak(ws)
            receive_until(ws, "not_woken")
            speak(ws)
            receive_until(ws, "wake")
            speak(ws)
            result, _ = receive_until(ws, "action_result")
        self.assertEqual(result["text"], "buy one hundred dollars of nvidia")
        self.assertEqual(main.transcripts, [])

    def test_a_request_said_with_the_wake_word_uses_the_main_recogniser(self):
        main = ScriptedRecognizer(["Hey Dimon, send Alicia two dollars from savings"])
        local = ScriptedRecognizer(["Hey Dimmon, send a leisure two dollars from saving"])
        client, dispatched, _ = build([], recognizer=main, wake_recognizer=local)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            speak(ws)
            result, _ = receive_until(ws, "action_result")
        self.assertEqual(result["text"], "send Alicia two dollars from savings")

    def test_wake_falls_back_to_the_main_recogniser_when_the_local_one_fails(self):
        class Broken:
            async def transcribe(self, audio, on_partial):
                raise ConnectionError("whisper.cpp is not running")

        client, _, _ = build(["hey dimon"], wake_recognizer=Broken())
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            speak(ws)
            wake, _ = receive_until(ws, "wake")
        self.assertEqual(wake["type"], "wake")

    def test_a_registration_sample_must_be_the_phrase(self):
        recorded = []
        ledger = FakeLedger()
        ledger.add_voice_enrollment_sample = lambda enrollment_id, audio: recorded.append(enrollment_id) or {
            "samples": len(recorded), "samples_needed": 3, "complete": False}
        # Dimon's own prompt picked up by the microphone, then the phrase.
        client, _, _ = build(["Please say: Every payment needs my own voice. I see 2 saved payees named John",
                              "Every payment needs my own voice"], ledger)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            for expected in ("enroll_rejected", "enroll_sample"):
                ws.send_json({"type": "listen", "expect": "enroll", "enrollment_id": "venroll_1"})
                receive_until(ws, "listening")
                speak(ws)
                message, _ = receive_until(ws, expected)
            self.assertEqual(message["samples"], 1)
        self.assertEqual(recorded, ["venroll_1"])

    def test_a_stranger_cannot_wake_a_registered_voice(self):
        ledger = FakeLedger(registered=True, similarity=0.1)
        client, dispatched, _ = build(["hey dimon send mallory five hundred dollars"], ledger)
        with client.websocket_connect("/ws") as ws:
            self.assertTrue(ws.receive_json()["voiceprint"])
            speak(ws, "stranger")
            ignored, seen = receive_until(ws, "ignored")
        self.assertEqual(ignored["reason_code"], "SPEAKER_MISMATCH")
        self.assertNotIn("mallory", json.dumps(seen).lower())
        self.assertEqual(dispatched, [])

    def test_the_account_holder_wakes_with_a_matched_voice(self):
        ledger = FakeLedger(registered=True, similarity=0.8)
        client, _, _ = build(["hey dimon"], ledger)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            speak(ws)
            wake, _ = receive_until(ws, "wake")
        self.assertEqual(wake["speaker"], "MATCH")

    def test_replies_are_interpreted_not_dispatched(self):
        client, dispatched, _ = build(["yes please go ahead", "from my everyday account"])
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"type": "listen", "expect": "confirm"})
            receive_until(ws, "listening")
            speak(ws)
            reply, _ = receive_until(ws, "reply")
            self.assertIs(reply["value"], True)
            ws.send_json({"type": "listen", "expect": "account", "options": ["savings", "everyday"]})
            receive_until(ws, "listening")
            speak(ws)
            reply, _ = receive_until(ws, "reply")
            self.assertEqual(reply["value"], "everyday")
        self.assertEqual(dispatched, [])

    def test_follow_up_replies_emit_main_recognizer_live_captions(self):
        main = PartialRecognizer(["last for today"])
        local = ScriptedRecognizer(["unused local transcript"])
        bridge = HandsFreeBridge(
            recognizer=main,
            lease=VoiceLease(),
            wake_recognizer=local,
            wake_lease=VoiceLease(),
        )
        partials = []

        async def scenario():
            async def on_partial(text):
                partials.append(text)

            return await bridge._transcribe(b"reply audio", local=False, on_partial=on_partial)

        self.assertEqual(asyncio.run(scenario()), "last for today")
        self.assertEqual(partials, ["last for", "last for today"])
        self.assertEqual(local.transcripts, ["unused local transcript"])

    def test_spoken_confirmation_goes_to_voice_verification(self):
        client, dispatched, ledger = build(["four seven one nine", "one two three four"])
        challenge = "chal_" + "0" * 32
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"type": "listen", "expect": "approval", "challenge_id": challenge})
            receive_until(ws, "listening")
            speak(ws)
            verified, _ = receive_until(ws, "voice_verified")
            self.assertEqual(verified["verdict"], "MATCH")
            ws.send_json({"type": "listen", "expect": "approval", "challenge_id": challenge})
            receive_until(ws, "listening")
            speak(ws)
            rejected, _ = receive_until(ws, "voice_rejected")
        self.assertEqual(rejected["reason_code"], "VOICE_PHRASE_MISMATCH")
        self.assertEqual(dispatched, [])
        self.assertEqual([c for c, _ in ledger.verified], [challenge, challenge])

    def test_malformed_control_frames_end_the_session(self):
        client, _, _ = build([])
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"type": "listen", "expect": "transfer_everything"})
            self.assertEqual(ws.receive_json(), {"type": "error", "reason_code": "MALFORMED_FRAME"})


class ChoiceTests(unittest.TestCase):
    def test_a_misheard_surname_still_picks_the_payee(self):
        from apps.dimon_live.voice_dialog import choice

        options = ["John Smith", "John Tan"]
        for heard, picked in (("John 10", 1), ("John ten", 1), ("Tan", 1), ("Smit", 0), ("Jontan", 1),
                              ("the second one", 1), ("John Smith", 0)):
            with self.subTest(heard=heard):
                self.assertEqual(choice(heard, options), picked)

    def test_an_unclear_answer_picks_nobody(self):
        from apps.dimon_live.voice_dialog import choice

        for heard in ("John", "yes", "hmm", ""):
            with self.subTest(heard=heard):
                self.assertIsNone(choice(heard, ["John Smith", "John Tan"]))


class WakePhraseTests(unittest.TestCase):
    def test_wakes_on_how_people_and_recognisers_say_it(self):
        from apps.dimon_live.voice_dialog import wake

        for heard, command in (
            ("Hey Dimmon, send John Smith $3 from savings.", "send John Smith $3 from savings"),
            ("Hey, Dimin, send mum $20", "send mum $20"),
            ("HeyDimon send John", "send John"),
            ("Um, hey Dimon, pay Alicia", "pay Alicia"),
            ("Dimon, send John $5", "send John $5"),
            ("Yo Damon send John", "send John"),
            ("Okay Dimon", ""),
            # Heard by whisper.cpp (small.en) from synthetic voices.
            ("Hey demon!", ""),
            ("High Dimmon", ""),
            ("Hydiman", ""),
            ("Hydimmon", ""),
            ("Hademon", ""),
            ("Hedymon", ""),
            ("Hae Dimun", ""),
            ("He Ji Man", ""),
            ("Hey, Jimin", ""),
            ("Hey Dimin, what's my balance?", "what's my balance"),
            ("Okay, hey Dimon", ""),
        ):
            with self.subTest(heard=heard):
                self.assertEqual(wake(heard), (True, command))

    def test_ignores_everyday_speech_and_real_names(self):
        from apps.dimon_live.voice_dialog import wake

        for heard in ("Damon, are you coming?", "Simon, can you pass the salt", "Diamond rings are nice",
                      "Demons are scary", "Dim on the lights", "Hey, how are you doing?", "Hello there",
                      "He demands a refund", "High demand today", "Heidi, Monday works", "Hey Tim, on Monday",
                      "He timed out", "Hey dinner time", "Hi Diana", "Hey Dimitri", "Hydrogen", "Happy Monday"):
            with self.subTest(heard=heard):
                self.assertFalse(wake(heard)[0])


if __name__ == "__main__":
    unittest.main()
