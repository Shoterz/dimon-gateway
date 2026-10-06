"""The hands-free socket: wake word, follow-ups, spoken confirmation."""

import unittest
from types import SimpleNamespace

from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from apps.dimon_live.hands_free import HandsFreeBridge, Segmenter
from apps.dimon_live.voice import VoiceLease
from apps.dimon_live.voiceprint import VoiceRejected, digits_heard
from apps.dimon_live.tests.voice_fake import clip

CHUNK = 4096


class ScriptedRecognizer:
    """Returns the next scripted transcript for each utterance."""

    def __init__(self, transcripts):
        self.transcripts = list(transcripts)

    async def transcribe(self, audio, on_partial):
        return self.transcripts.pop(0) if self.transcripts else ""


class FakeLedger:
    def __init__(self, *, registered=False, similarity=0.9):
        self.registered = registered
        self.similarity_by_speaker = similarity
        self.verified = []
        self.voice_store = SimpleNamespace(speaker_similarity=self._similarity)
        self.next_speaker_similarity = similarity

    def voice_status(self):
        return {"voice_registered": self.registered, "voice_max_minor": 20_000}

    def _similarity(self, user_id, audio):
        return self.next_speaker_similarity

    def verify_voice(self, challenge_id, audio, transcript):
        self.verified.append((challenge_id, transcript))
        if "4719" not in digits_heard(transcript):
            raise VoiceRejected("VOICE_PHRASE_MISMATCH")
        return {"sample_id": "voice_1", "verdict": "MATCH", "similarity": 0.91}

    def add_voice_enrollment_sample(self, enrollment_id, audio):
        return {"samples": 1, "samples_needed": 3, "complete": False}


def build(transcripts, ledger=None):
    ledger = ledger or FakeLedger()
    dispatched = []
    bridge = HandsFreeBridge(recognizer=ScriptedRecognizer(transcripts), lease=VoiceLease())
    app = FastAPI()

    @app.websocket("/ws")
    async def socket(websocket: WebSocket):
        async def dispatch(text, evidence):
            dispatched.append((text, evidence))
            return {"outcome": "ANSWERED", "answer": f"did {text}"}

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

    def test_silence_alone_is_never_an_utterance(self):
        segmenter = Segmenter()
        silence = bytes(CHUNK * 40)
        for offset in range(0, len(silence), CHUNK):
            self.assertEqual(segmenter.push(silence[offset:offset + CHUNK]), (None, None))


class HandsFreeTests(unittest.TestCase):
    def test_speech_without_the_wake_word_is_dropped_silently(self):
        client, dispatched, _ = build(["send alicia two dollars"])
        with client.websocket_connect("/ws") as ws:
            self.assertEqual(ws.receive_json()["type"], "ready")
            speak(ws)
            ws.send_json({"type": "listen", "expect": "command"})
            # Nothing about the dropped utterance comes back.
            self.assertEqual(ws.receive_json()["type"], "listening")
        self.assertEqual(dispatched, [])

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

    def test_wake_word_alone_then_the_command(self):
        client, dispatched, _ = build(["hey dimon", "buy one hundred dollars of nvidia"])
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            speak(ws)
            receive_until(ws, "wake")
            speak(ws)
            result, _ = receive_until(ws, "action_result")
        self.assertEqual(result["text"], "buy one hundred dollars of nvidia")

    def test_a_stranger_cannot_wake_a_registered_voice(self):
        ledger = FakeLedger(registered=True, similarity=0.1)
        client, dispatched, _ = build(["hey dimon send mallory five hundred dollars"], ledger)
        with client.websocket_connect("/ws") as ws:
            self.assertTrue(ws.receive_json()["voiceprint"])
            speak(ws, "stranger")
            ws.send_json({"type": "sleep"})
            ws.send_json({"type": "listen", "expect": "command"})
            self.assertEqual(ws.receive_json()["type"], "listening")
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


if __name__ == "__main__":
    unittest.main()
