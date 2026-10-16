import json
import re
import subprocess
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
CLIENT_MODULE = PROJECT_ROOT / "apps" / "dimon_live" / "static" / "receipt_approval.mjs"
BATCH_CLIENT_MODULE = PROJECT_ROOT / "apps" / "dimon_live" / "static" / "batch_receipt_approval.mjs"
CLARIFICATION_MODULE = (
    PROJECT_ROOT / "apps" / "dimon_live" / "static" / "clarification_response.mjs"
)
ACTION_MODULE = PROJECT_ROOT / "apps" / "dimon_live" / "static" / "action_request.mjs"
ACCOUNT_DISPLAY_MODULE = (
    PROJECT_ROOT / "apps" / "dimon_live" / "static" / "account_display.mjs"
)
TRANSACTION_DISPLAY_MODULE = (
    PROJECT_ROOT / "apps" / "dimon_live" / "static" / "transaction_display.mjs"
)
VOICE_CAPTURE_MODULE = (
    PROJECT_ROOT / "apps" / "dimon_live" / "static" / "voice_capture.mjs"
)
VOICE_FAILURE_MODULE = (
    PROJECT_ROOT / "apps" / "dimon_live" / "static" / "voice_failure.mjs"
)
HANDS_FREE_MODULE = (
    PROJECT_ROOT / "apps" / "dimon_live" / "static" / "hands_free.mjs"
)
RESULT_AUDIO_MODULE = (
    PROJECT_ROOT / "apps" / "dimon_live" / "static" / "result_audio.mjs"
)
AUDIO_PREBUFFER_MODULE = (
    PROJECT_ROOT / "apps" / "dimon_live" / "static" / "audio_prebuffer.mjs"
)
IDENTITY_FEEDBACK_MODULE = (
    PROJECT_ROOT / "apps" / "dimon_live" / "static" / "identity_feedback.mjs"
)
BANKING_SHELL_MODULE = (
    PROJECT_ROOT / "apps" / "dimon_live" / "static" / "banking_shell.mjs"
)
INVEST_UI_MODULE = PROJECT_ROOT / "apps" / "dimon_live" / "static" / "invest_ui.mjs"
APP_CLIENT = PROJECT_ROOT / "apps" / "dimon_live" / "static" / "app.js"
INDEX_PAGE = PROJECT_ROOT / "apps" / "dimon_live" / "static" / "index.html"
APP_STYLES = PROJECT_ROOT / "apps" / "dimon_live" / "static" / "app.css"
WAKE_GREETING_AUDIO = (
    PROJECT_ROOT / "apps" / "dimon_live" / "static" / "audio" / "wake-greeting-v1.wav"
)
VOICE_ENROLLMENT_PROMPT_AUDIO = (
    PROJECT_ROOT / "apps" / "dimon_live" / "static" / "audio"
    / "voice-enrollment-confirmation-v1.wav"
)


class ReceiptApprovalClientTests(unittest.TestCase):
    def test_prompt_injection_block_uses_dedicated_hands_free_speech(self):
        program = f"""
          import {{ blockedResultSpeech }} from {json.dumps(HANDS_FREE_MODULE.as_uri())};
          const injection = blockedResultSpeech({{
            outcome: "BLOCKED", reason_code: "PROMPT_INJECTION_BLOCKED",
          }});
          const other = blockedResultSpeech({{
            outcome: "BLOCKED", reason_code: "APP_LOCKED",
          }});
          process.stdout.write(JSON.stringify({{injection, other}}));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program], cwd=PROJECT_ROOT,
            capture_output=True, check=False, text=True, encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {
            "injection": {
                "promptKey": "prompt_injection_blocked",
                "text": "I couldn’t do that because prompt injection was blocked. Nothing was sent.",
            },
            "other": {"promptKey": "check_screen", "text": None},
        })

    def test_failed_early_wake_returns_browser_to_listening(self):
        program = f"""
          import {{ wakeConfirmationTransition }} from {json.dumps(HANDS_FREE_MODULE.as_uri())};
          const detected = wakeConfirmationTransition(false, {{
            type: "wake", path: "nemotron_partial",
          }});
          const failed = wakeConfirmationTransition(detected.pending, {{type: "error"}});
          const missed = wakeConfirmationTransition(detected.pending, {{type: "not_woken"}});
          const completed = wakeConfirmationTransition(detected.pending, {{type: "wake_complete"}});
          process.stdout.write(JSON.stringify({{detected, failed, missed, completed}}));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program], cwd=PROJECT_ROOT,
            capture_output=True, check=False, text=True, encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {
            "detected": {"pending": True, "recover": False, "resume": False},
            "failed": {"pending": False, "recover": True, "resume": True},
            "missed": {"pending": False, "recover": True, "resume": True},
            "completed": {"pending": False, "recover": False, "resume": False},
        })

    def test_voice_enrollment_is_single_flight(self):
        program = f"""
          import {{ createSingleFlight }} from {json.dumps(HANDS_FREE_MODULE.as_uri())};
          let starts = 0;
          let release;
          const gate = new Promise(resolve => {{ release = resolve; }});
          const run = createSingleFlight(async () => {{
            starts += 1;
            await gate;
            return "registered";
          }});
          const first = run();
          const second = run();
          const samePromise = first === second;
          release();
          const results = await Promise.all([first, second]);
          await run();
          process.stdout.write(JSON.stringify({{starts, samePromise, results}}));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program], cwd=PROJECT_ROOT,
            capture_output=True, check=False, text=True, encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {
            "starts": 2,
            "samePromise": True,
            "results": ["registered", "registered"],
        })

    def test_d_warm_unlocks_audio_before_fetch_await(self):
        program = f"""
          import {{ beginWarmupFromGesture }} from {json.dumps(HANDS_FREE_MODULE.as_uri())};
          const events = [];
          let releaseVoice;
          const voiceGate = new Promise(resolve => {{ releaseVoice = resolve; }});
          const pending = beginWarmupFromGesture(
            () => {{ events.push("audio-resume-started"); return voiceGate; }},
            () => {{ events.push("backend-fetch-started"); return Promise.resolve("warm"); }},
          );
          const beforeAwait = [...events];
          releaseVoice("voice");
          const result = await pending;
          process.stdout.write(JSON.stringify({{beforeAwait, result}}));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program], cwd=PROJECT_ROOT,
            capture_output=True, check=False, text=True, encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {
            "beforeAwait": ["audio-resume-started", "backend-fetch-started"],
            "result": {"voice": "voice", "backend": "warm"},
        })

    def test_d_reset_does_not_start_microphone_until_backend_is_ready(self):
        program = f"""
          import {{ beginResetWarmupFromGesture }} from {json.dumps(HANDS_FREE_MODULE.as_uri())};
          const events = [];
          let releaseBackend;
          const backendGate = new Promise(resolve => {{ releaseBackend = resolve; }});
          const pending = beginResetWarmupFromGesture(
            () => {{ events.push("audio-unlocked"); return Promise.resolve("audio"); }},
            () => {{ events.push("gpu-reset-started"); return backendGate; }},
            () => {{ events.push("microphone-started"); return Promise.resolve("voice"); }},
          );
          const duringReset = [...events];
          releaseBackend({{state: "READY"}});
          const result = await pending;
          process.stdout.write(JSON.stringify({{duringReset, events, result}}));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program], cwd=PROJECT_ROOT,
            capture_output=True, check=False, text=True, encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {
            "duringReset": ["audio-unlocked", "gpu-reset-started"],
            "events": ["audio-unlocked", "gpu-reset-started", "microphone-started"],
            "result": {"voice": "voice", "backend": {"state": "READY"}},
        })

    def test_green_ready_requires_backend_audio_microphone_and_socket(self):
        program = f"""
          import {{ combinedWarmPresentation }} from {json.dumps(HANDS_FREE_MODULE.as_uri())};
          const ready = {{state: "READY"}};
          const full = {{state: "READY", audio: true, microphone: true, socket: true}};
          const cases = [
            combinedWarmPresentation(ready, full),
            combinedWarmPresentation(ready, {{...full, audio: false}}),
            combinedWarmPresentation(ready, {{...full, microphone: false}}),
            combinedWarmPresentation(ready, {{...full, socket: false}}),
            combinedWarmPresentation({{state: "PREPARING"}}, full),
            combinedWarmPresentation(ready, {{...full, state: "STARTING", audio: false, needsGesture: true}}),
          ];
          process.stdout.write(JSON.stringify(cases));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program], cwd=PROJECT_ROOT,
            capture_output=True, check=False, text=True, encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        cases = json.loads(completed.stdout)
        self.assertEqual(cases[0]["state"], "ready")
        self.assertEqual(cases[0]["status"], 'Ready — say “Hey Dimon”')
        self.assertEqual([case["state"] for case in cases[1:4]], ["preparing"] * 3)
        self.assertEqual([case["status"] for case in cases[1:4]], ["Starting microphone"] * 3)
        self.assertEqual(cases[4]["status"], "Warming services")
        self.assertEqual(cases[5], {
            "state": "cold",
            "label": "Start Dimon’s microphone",
            "status": "Tap D to start microphone",
        })

    def test_active_voice_state_overrides_incomplete_background_readiness(self):
        program = f"""
          import {{ combinedWarmPresentation }} from {json.dumps(HANDS_FREE_MODULE.as_uri())};
          const backend = {{state: "READY"}};
          const incomplete = {{state: "STARTING", audio: true, microphone: true, socket: false}};
          process.stdout.write(JSON.stringify([
            combinedWarmPresentation(backend, incomplete, "listening"),
            combinedWarmPresentation(backend, incomplete, "understanding"),
          ]));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program], cwd=PROJECT_ROOT,
            capture_output=True, check=False, text=True, encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), [
            {
                "state": "ready",
                "label": "Dimon is listening",
                "status": "Listening",
                "active": True,
            },
            {
                "state": "ready",
                "label": "Dimon is processing your request",
                "status": "Processing request",
                "active": True,
            },
        ])

    def test_hands_free_conversation_context_message_is_bounded_and_clearable(self):
        program = f"""
          import {{ conversationContextMessage }} from {json.dumps(HANDS_FREE_MODULE.as_uri())};
          const waiting = conversationContextMessage({{
            status: "WAITING", conversation_id: "conversation_nvda", version: 2,
          }});
          const completed = conversationContextMessage({{
            status: "COMPLETED", conversation_id: "conversation_nvda", version: 3,
          }});
          process.stdout.write(JSON.stringify({{waiting, completed}}));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program], cwd=PROJECT_ROOT,
            capture_output=True, check=False, text=True, encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {
            "waiting": {
                "type": "conversation_context",
                "conversation_id": "conversation_nvda",
                "expected_version": 2,
            },
            "completed": {
                "type": "conversation_context",
                "conversation_id": None,
                "expected_version": None,
            },
        })

    def test_bill_correction_and_duplicate_greeting_are_recognized(self):
        program = f"""
          import {{
            isExplicitBillRequest,
            shouldAppendTranscriptTurn,
          }} from {json.dumps(HANDS_FREE_MODULE.as_uri())};
          process.stdout.write(JSON.stringify({{
            plural: isExplicitBillRequest("I want to pay my bills"),
            correction: isExplicitBillRequest("No, just pay my water bill"),
            person: isExplicitBillRequest("John Tan"),
            duplicate: shouldAppendTranscriptTurn(
              "assistant", "Hi, I'm Dimon. How can I help you?",
              "assistant", "Hi, I'm Dimon. How can I help you?",
            ),
            equivalentWelcome: shouldAppendTranscriptTurn(
              "assistant", "I can help with your accounts, payments, and investments. What would you like to do?",
              "assistant", "Hi, I'm Dimon. How can I help you?",
            ),
            userTurn: shouldAppendTranscriptTurn(
              "assistant", "Hi, I'm Dimon. How can I help you?",
              "user", "Buy one NVIDIA share",
            ),
          }}));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program], cwd=PROJECT_ROOT,
            capture_output=True, check=False, text=True, encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {
            "plural": True,
            "correction": True,
            "person": False,
            "duplicate": False,
            "equivalentWelcome": False,
            "userTurn": True,
        })

    def test_partial_caption_is_provisional_and_final_caption_is_authoritative(self):
        program = f"""
          import {{ voiceCaptionPresentation }} from {json.dumps(HANDS_FREE_MODULE.as_uri())};
          process.stdout.write(JSON.stringify([
            voiceCaptionPresentation("transcript_partial", "buy one share of nvidia"),
            voiceCaptionPresentation("transcript_final", "I want to buy one share of NVIDIA"),
          ]));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program], cwd=PROJECT_ROOT,
            capture_output=True, check=False, text=True, encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), [
            {
                "text": "Hearing: “buy one share of nvidia”",
                "provisional": True,
                "ariaLive": "off",
            },
            {
                "text": "I want to buy one share of NVIDIA",
                "provisional": False,
                "ariaLive": "polite",
            },
        ])

    def test_demo_exchange_disclosure_names_closed_market_and_no_real_money(self):
        program = f"""
          import {{ demoExchangeDisclosure }} from {json.dumps(HANDS_FREE_MODULE.as_uri())};
          process.stdout.write(JSON.stringify([
            demoExchangeDisclosure({{demo_override: true, actual_market_state: "CLOSED"}}),
            demoExchangeDisclosure({{demo_override: false, actual_market_state: "OPEN"}}),
          ]));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program], cwd=PROJECT_ROOT,
            capture_output=True, check=False, text=True, encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), [
            "Simulated fill using the latest timestamped quote · Nasdaq was closed · no real money",
            None,
        ])

    def test_first_frames_before_socket_ready_are_prebuffered(self):
        program = f"""
          import {{ RollingAudioPrebuffer }} from {json.dumps(AUDIO_PREBUFFER_MODULE.as_uri())};
          const buffer = new RollingAudioPrebuffer(64_000);
          for (let value = 1; value <= 5; value += 1) buffer.push(new Uint8Array(16_000).fill(value).buffer);
          const frames = buffer.drain().map(frame => new Uint8Array(frame));
          process.stdout.write(JSON.stringify({{
            bytes: frames.reduce((total, frame) => total + frame.byteLength, 0),
            first: frames[0][0],
            last: frames.at(-1).at(-1),
          }}));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program], cwd=PROJECT_ROOT,
            capture_output=True, check=False, text=True, encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"bytes": 64_000, "first": 2, "last": 5})

    def test_audio_prebuffer_retains_only_latest_second_in_order(self):
        program = f"""
          import {{ RollingAudioPrebuffer }} from {json.dumps(AUDIO_PREBUFFER_MODULE.as_uri())};
          const buffer = new RollingAudioPrebuffer(8);
          buffer.push(Uint8Array.from([1, 2, 3, 4]).buffer);
          buffer.push(Uint8Array.from([5, 6, 7, 8, 9, 10]).buffer);
          const drained = buffer.drain().flatMap(frame => [...new Uint8Array(frame)]);
          process.stdout.write(JSON.stringify({{drained, bytes: buffer.byteLength}}));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program], cwd=PROJECT_ROOT,
            capture_output=True, check=False, text=True, encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"drained": [3, 4, 5, 6, 7, 8, 9, 10], "bytes": 0})

    def test_audio_prebuffer_drain_and_clear_remove_old_frames(self):
        program = f"""
          import {{ RollingAudioPrebuffer }} from {json.dumps(AUDIO_PREBUFFER_MODULE.as_uri())};
          const buffer = new RollingAudioPrebuffer(64000);
          const source = Uint8Array.from([7, 8]).buffer;
          buffer.push(source);
          new Uint8Array(source)[0] = 99;
          const first = buffer.drain().flatMap(frame => [...new Uint8Array(frame)]);
          buffer.push(Uint8Array.from([1, 2]).buffer);
          buffer.clear();
          process.stdout.write(JSON.stringify({{first, second: buffer.drain(), bytes: buffer.byteLength}}));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program], cwd=PROJECT_ROOT,
            capture_output=True, check=False, text=True, encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"first": [7, 8], "second": [], "bytes": 0})

    def test_hands_free_prebuffer_waits_for_ready_and_clears_on_audio_suppression(self):
        source = HANDS_FREE_MODULE.read_text(encoding="utf-8")
        self.assertIn('from "./audio_prebuffer.mjs"', source)
        self.assertIn("startupAudio.push", source)
        self.assertIn('type: "prebuffer_flushed"', source)
        self.assertIn("flushStartupAudio(current)", source)
        self.assertIn("startupAudio.clear()", source)
        self.assertIn("if (dimonAudible", source)

    def test_wake_greeting_is_a_shipped_wav_preloaded_from_browser_cache(self):
        hands_free = HANDS_FREE_MODULE.read_text(encoding="utf-8")

        self.assertTrue(WAKE_GREETING_AUDIO.read_bytes().startswith(b"RIFF"))
        self.assertIn('const WAKE_GREETING_AUDIO_URL = "/static/audio/wake-greeting-v1.wav";', hands_free)
        self.assertIn('cache: "force-cache"', hands_free)
        self.assertIn("preloadWakeGreeting();", hands_free)
        self.assertIn('if (promptKey === "wake_greeting")', hands_free)

    def test_first_voice_enrollment_prompt_is_a_shipped_preloaded_wav(self):
        hands_free = HANDS_FREE_MODULE.read_text(encoding="utf-8")

        self.assertTrue(VOICE_ENROLLMENT_PROMPT_AUDIO.read_bytes().startswith(b"RIFF"))
        self.assertIn(
            'const VOICE_ENROLLMENT_PROMPT_AUDIO_URL = "/static/audio/voice-enrollment-confirmation-v1.wav";',
            hands_free,
        )
        self.assertIn("preloadVoiceEnrollmentPrompt();", hands_free)
        self.assertIn('if (promptKey === "enrollment_prompt" && values.index === 0)', hands_free)
        self.assertIn('fetch(VOICE_ENROLLMENT_PROMPT_AUDIO_URL, {cache: "force-cache"})', hands_free)

    def test_start_new_chat_clears_visible_and_pending_conversation_only(self):
        source = APP_CLIENT.read_text(encoding="utf-8")
        markup = INDEX_PAGE.read_text(encoding="utf-8")

        self.assertIn('id="new-request-button"', markup)
        self.assertIn(">Start new chat</button>", markup)
        self.assertIn("function resetChatTranscript()", source)
        self.assertIn("ui.assistantTranscript.replaceChildren(welcome, ui.outcomePanel);", source)
        self.assertIn("activeMultiActionResponse = null;", source)
        self.assertIn("multiRequest.resetPlan();", source)
        self.assertIn("resetChatTranscript();", source)
        self.assertIn('ui.conversationStatus.textContent = "New chat started. Previous messages are no longer shown.";', source)

    def test_multi_request_plan_can_be_cleared_for_a_new_chat(self):
        program = """
          import {hasPlan, resetPlan, startPlan} from './apps/dimon_live/static/multi_request.mjs';
          startPlan({steps: [{text: 'buy NVIDIA', result: {outcome: 'ANSWERED'}}]});
          const before = hasPlan();
          resetPlan();
          process.stdout.write(JSON.stringify({before, after: hasPlan()}));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program], cwd=PROJECT_ROOT,
            capture_output=True, check=False, text=True, encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"before": True, "after": False})

    def test_single_recipient_match_requires_spoken_confirmation(self):
        program = f"""
          import {{ recipientClarificationPlan }} from {json.dumps(HANDS_FREE_MODULE.as_uri())};
          const plan = recipientClarificationPlan({{
            outcome: "CLARIFICATION_REQUEST",
            reason_code: "CLOSE_RECIPIENT_MATCH",
            question: "Did you mean Alicia Powell ending in 9876 (paid S$12 3 days ago)?",
            receipt_choices: [{{recipient_display_name: "Alicia Powell"}}],
          }});
          process.stdout.write(JSON.stringify(plan));
        """

        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "mode": "confirm",
                "question": "Did you mean Alicia Powell ending in 9876 (paid S$12 3 days ago)?",
                "names": ["Alicia Powell"],
            },
        )

    def test_ready_preparation_keeps_polling_for_keepalive_health(self):
        source = APP_CLIENT.read_text(encoding="utf-8")

        self.assertIn('state === "READY" || state === "DEGRADED"', source)
        self.assertIn("refreshPreparation(false, measuredTraceId), 15_000", source)
        self.assertIn("preparationStatusClearTimer", source)

    def test_partial_wake_opens_ask_without_pausing_authoritative_capture(self):
        hands_free = HANDS_FREE_MODULE.read_text(encoding="utf-8")
        app = APP_CLIENT.read_text(encoding="utf-8")

        partial_start = hands_free.index('message.path === "nemotron_partial"')
        partial_branch = hands_free[
            partial_start:
            hands_free.index('message.type === "wake_complete"', partial_start)
        ]
        self.assertIn("hooks.onWakeDetected?.()", partial_branch)
        self.assertNotIn("pause()", partial_branch)
        self.assertNotIn('send({type: "sleep"})', partial_branch)
        self.assertIn("onWakeDetected()", app)

    def test_finished_conversation_reopens_the_wake_microphone_gate(self):
        program = f"""
          import {{ returnToWakeListening }} from {json.dumps(HANDS_FREE_MODULE.as_uri())};
          const events = [];
          let paused = true;
          returnToWakeListening({{
            wanted: true,
            setPaused(value) {{ paused = value; }},
            sendSleep() {{ events.push("sleep"); }},
            setStatus(value) {{ events.push(value); }},
            idleStatus: 'Listening for “Hey Dimon”',
          }});
          process.stdout.write(JSON.stringify({{paused, events}}));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program], cwd=PROJECT_ROOT,
            capture_output=True, check=False, text=True, encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {
            "paused": False,
            "events": ["sleep", 'Listening for “Hey Dimon”'],
        })

    def test_recipient_reply_classifier_closes_social_or_cancel_replies(self):
        program = f"""
          import {{ classifyRecipientReply }} from {json.dumps(HANDS_FREE_MODULE.as_uri())};
          const values = ["Thank you", "thanks a lot", "never mind", "How was your day?", "hello there", "pizza", "Alicia Powell"]
            .map(classifyRecipientReply);
          process.stdout.write(JSON.stringify(values));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program], cwd=PROJECT_ROOT,
            capture_output=True, check=False, text=True, encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), [
            {"kind": "social"}, {"kind": "social"}, {"kind": "cancel"},
            {"kind": "social"}, {"kind": "social"},
            {"kind": "unclear"},
            {"kind": "recipient", "value": "Alicia Powell"},
        ])

    def test_shipped_clients_use_server_audio_not_browser_speech(self):
        static_dir = PROJECT_ROOT / "apps" / "dimon_live" / "static"
        forbidden = (
            "speechSynthesis",
            "SpeechSynthesisUtterance",
            "SpeechRecognition",
            "webkitSpeechRecognition",
        )
        for path in (*static_dir.glob("*.js"), *static_dir.glob("*.mjs")):
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                for api in forbidden:
                    self.assertNotIn(api, source)

        hands_free = (static_dir / "hands_free.mjs").read_text(encoding="utf-8")
        app = APP_CLIENT.read_text(encoding="utf-8")
        self.assertIn('fetch("/api/speech/prompts"', hands_free)
        self.assertIn("/api/audio/${encodeURIComponent(artifactId)}", hands_free)
        self.assertNotIn("new Audio(URL.createObjectURL", hands_free)
        self.assertIn("hooks.playSpeechBlob", hands_free)
        self.assertIn("playAudioToCompletion", app)
        self.assertIn("result.audio_artifact_id", hands_free)
        self.assertIn('message.type === "transcript_partial"', hands_free)
        self.assertIn("audio_artifact_id: payload.audio_artifact_id", app)

    def test_evidence_hierarchy_puts_current_proof_before_technical_detail(self):
        page = INDEX_PAGE.read_text(encoding="utf-8")

        ordered_ids = [
            'id="evidence-proof-summary"',
            'id="evidence-validation"',
            'id="evidence-receipt"',
            'id="commit-panel"',
            'id="evidence-exactly-once"',
            'id="evidence-diagnostics"',
            'id="evidence-playbook"',
            'id="intent-history"',
        ]
        positions = [page.index(marker) for marker in ordered_ids]
        self.assertEqual(positions, sorted(positions))

    def test_evidence_progressive_sections_use_native_disclosure(self):
        page = INDEX_PAGE.read_text(encoding="utf-8")

        self.assertRegex(page, r'<details id="evidence-diagnostics"[^>]*>\s*<summary')
        self.assertRegex(page, r'<details id="evidence-playbook"[^>]*>\s*<summary')
        self.assertRegex(page, r'<details id="intent-history"[^>]*>\s*<summary')
        self.assertNotRegex(page, r'<details id="(?:evidence-diagnostics|evidence-playbook|intent-history)"[^>]*\sopen(?:\s|>)')

    def test_evidence_contrast_uses_coherent_light_bank_tokens(self):
        styles = APP_STYLES.read_text(encoding="utf-8")
        evidence_styles = styles[styles.index("/* Accessible Evidence surface */"):]

        for token in ("--bank-surface", "--bank-graphite", "--bank-slate", "--bank-line", "--focus"):
            self.assertIn(f"var({token})", evidence_styles)
        self.assertNotIn("var(--overlay-panel-58)", evidence_styles)
        self.assertNotIn("var(--overlay-dark-76)", evidence_styles)
        self.assertIn(".evidence-state-label", evidence_styles)

    def test_customer_safe_error_copy_excludes_runtime_implementation_details(self):
        source = APP_CLIENT.read_text(encoding="utf-8")

        self.assertNotIn('ui.voiceStatus.textContent = `${message.reason_code}', source)
        self.assertNotRegex(source, r"setOutcome\([^;]+error\.message")
        self.assertNotIn("facePrompt.textContent = error.message", source)
        self.assertNotIn("Gateway unavailable", source)
        self.assertNotIn("Start the FastAPI service", source)
        self.assertIn("We couldn’t confirm the result", source)
        self.assertIn("Your balances may already reflect this request", source)

    def test_evidence_diagnostic_boundary_keeps_bounded_runtime_fields(self):
        source = APP_CLIENT.read_text(encoding="utf-8")

        self.assertIn("function recordEvidenceDiagnostic(stage, error = {})", source)
        self.assertIn("stage: String(stage || \"unknown\").slice(0, 64)", source)
        self.assertIn("errorName: String(error.name || \"Error\").slice(0, 64)", source)
        self.assertIn("reasonCode: String(error.reasonCode || error.reason_code || \"\").slice(0, 96)", source)
        self.assertIn("httpStatus: Number(error.httpStatus || error.status || 0) || null", source)
        self.assertIn('evidenceDiagnosticList: document.querySelector("#evidence-diagnostic-list")', source)
        self.assertIn('id="evidence-diagnostic-list"', INDEX_PAGE.read_text(encoding="utf-8"))
        self.assertNotIn("message: error.message", source)

    def test_unknown_action_recovery_exposes_all_safe_actions(self):
        source = APP_CLIENT.read_text(encoding="utf-8")

        self.assertIn("function renderUnknownActionRecovery(actionResponse, started)", source)
        self.assertIn('data-retry-same-request', source)
        self.assertIn('data-check-settlement-evidence', source)
        self.assertIn('data-discard-unknown-request', source)
        self.assertIn("Retry same request", source)
        self.assertIn("Check settlement evidence", source)
        self.assertIn("Discard and start over", source)
        self.assertIn('ui.newRequestButton.hidden = true', source)

    def test_identical_request_retry_reuses_coordinates_and_blocks_parallel_clicks(self):
        source = APP_CLIENT.read_text(encoding="utf-8")

        self.assertIn("let unknownActionRetryInFlight = false", source)
        self.assertIn("if (unknownActionRetryInFlight || !pendingActionRequest) return", source)
        self.assertIn("await requestAction(pendingActionRequest)", source)
        self.assertIn("control.disabled = true", source)
        retry_block = source[source.index("async function retryPendingActionRequest"):source.index("function renderUnknownActionRecovery")]
        self.assertNotIn("crypto.randomUUID", retry_block)

    def test_discard_unknown_request_warns_before_clearing_coordinates(self):
        source = APP_CLIENT.read_text(encoding="utf-8")
        recovery = source[source.index("function renderUnknownActionRecovery"):source.index("function enterClarificationMode")]

        confirmation = recovery.index("window.confirm")
        reset = recovery.index("resetCommandMode()")
        evidence = recovery.index("openEvidence(document, activeEvidenceContext)")
        self.assertLess(evidence, confirmation)
        self.assertLess(confirmation, reset)
        self.assertIn("previous outcome is still unknown", recovery)

    def test_invest_and_digiwealth_shortcuts_have_distinct_decorative_icons(self):
        markup = INDEX_PAGE.read_text(encoding="utf-8")
        invest = re.search(
            r'<button[^>]+data-prompt="Show my portfolio"[^>]*>.*?</button>', markup, re.S
        ).group(0)
        digiwealth = re.search(
            r'<button[^>]+data-view-link="wealth"[^>]*>.*?</button>', markup, re.S
        ).group(0)
        invest_icon = re.search(r"<svg.*?</svg>", invest, re.S).group(0)
        digiwealth_icon = re.search(r"<svg.*?</svg>", digiwealth, re.S).group(0)

        self.assertIn("<span>Invest</span>", invest)
        self.assertIn("<span>digiWealth</span>", digiwealth)
        self.assertIn('aria-hidden="true"', invest_icon)
        self.assertIn('aria-hidden="true"', digiwealth_icon)
        self.assertIn('focusable="false"', invest_icon)
        self.assertIn('focusable="false"', digiwealth_icon)
        self.assertNotEqual(invest_icon, digiwealth_icon)

    def test_startup_watchdog_is_independent_and_actionable(self):
        markup = INDEX_PAGE.read_text(encoding="utf-8")
        watchdog_start = markup.index('id="dimon-boot-watchdog"')
        module_start = markup.index('src="/static/app.js"')
        watchdog = markup[watchdog_start:module_start]

        self.assertLess(watchdog_start, module_start)
        self.assertNotIn('type="module"', watchdog)
        self.assertNotIn("import ", watchdog)
        self.assertIn('addEventListener("dimon:boot-complete"', watchdog)
        self.assertIn('window.__dimonBootWatchdog', watchdog)
        self.assertIn('Reload Dimon', watchdog)
        self.assertIn('role="alert"', watchdog)
        self.assertIn('accountList.classList.add("startup-failed")', watchdog)
        self.assertIn(
            ".deposit-product-card .home-account-list.startup-failed { display: grid; }",
            APP_STYLES.read_text(encoding="utf-8"),
        )

    def test_unexplained_balance_becomes_unavailable_when_startup_times_out(self):
        markup = INDEX_PAGE.read_text(encoding="utf-8")
        watchdog_start = markup.index('id="dimon-boot-watchdog"')
        module_start = markup.index('src="/static/app.js"')
        watchdog = markup[watchdog_start:module_start]

        self.assertIn('"home-total-balance", "home-wealth-balance"', watchdog)
        self.assertIn('balance.textContent = "Unavailable"', watchdog)
        self.assertIn('window.setTimeout(showBootFailure, 8000)', watchdog)

    def test_boot_complete_is_dispatched_only_after_required_startup_loads(self):
        source = APP_CLIENT.read_text(encoding="utf-8")
        startup = source[source.index("loadSession().then(session => {"):]

        self.assertIn("if (session?.locked)", startup)
        self.assertIn("return Promise.all([loadState(), loadPayees()]);", startup)
        self.assertIn('.then(() => {\n  document.dispatchEvent(new Event("dimon:boot-complete"));\n})', startup)
        self.assertLess(
            startup.index("return Promise.all([loadState(), loadPayees()]);"),
            startup.index('document.dispatchEvent(new Event("dimon:boot-complete"))'),
        )
        self.assertLess(
            startup.index('document.dispatchEvent(new Event("dimon:boot-complete"))'),
            startup.index(".catch(error => {"),
        )

    def test_lock_startup_completes_watchdog_without_loading_accounts(self):
        source = APP_CLIENT.read_text(encoding="utf-8")
        startup = source[source.index("loadSession().then(session => {"):]
        locked = startup[startup.index("if (session?.locked)"):startup.index("startUnlockedShell()")]

        self.assertIn("showLockScreen();", locked)
        self.assertIn("return;", locked)
        for unavailable_while_locked in (
            "loadState()",
            "loadPayees()",
            "loadInvestments()",
            "refreshIdentityStatus()",
            "startHandsFree()",
        ):
            self.assertNotIn(unavailable_while_locked, locked)
        self.assertIn("startUnlockedShell();", startup)
        self.assertIn('document.dispatchEvent(new Event("dimon:boot-complete"))', startup)

    def test_hands_free_pauses_push_to_talk_and_result_playback(self):
        source = APP_CLIENT.read_text(encoding="utf-8")
        hands_free = (PROJECT_ROOT / "apps" / "dimon_live" / "static" / "hands_free.mjs").read_text(
            encoding="utf-8"
        )
        start_voice = source[source.index("async function startVoice()"):source.index("async function startAudioWorkletVoice()")]
        audible = source[source.index("function dimonAudible()"):source.index("function speakReadBack")]

        self.assertIn("await wakeWord?.pauseForPushToTalk?.();", start_voice)
        self.assertLess(
            start_voice.index("pauseForPushToTalk"),
            start_voice.index("startAudioWorkletVoice"),
        )
        self.assertIn("resumeHandsFreeAfterPushToTalk", source)
        self.assertIn("playing(ui.resultAudio) || playing(readBackAudio)", audible)
        self.assertIn("isDimonAudible: dimonAudible", source)
        self.assertIn("async function pauseForPushToTalk()", hands_free)
        self.assertIn("stopAudio();", hands_free[hands_free.index("async function pauseForPushToTalk()"):])
        self.assertIn("async function resumeAfterPushToTalk()", hands_free)

    def test_bare_wake_paints_ask_view_before_playing_the_greeting(self):
        source = APP_CLIENT.read_text(encoding="utf-8")
        wake = source[source.index("async onWake() {"):source.index("async onCommand(result, text)")]

        self.assertIn("await nextAnimationFrame();", wake)
        self.assertLess(wake.index('activateView(document, "ask"'), wake.index("await nextAnimationFrame();"))
        self.assertLess(wake.index("await nextAnimationFrame();"), wake.index('wakeWord.say("wake_greeting"'))

    def test_lock_screen_stops_microphone_sockets_and_playback(self):
        source = APP_CLIENT.read_text(encoding="utf-8")
        hands_free = (PROJECT_ROOT / "apps" / "dimon_live" / "static" / "hands_free.mjs").read_text(
            encoding="utf-8"
        )
        show_lock = source[source.index("function showLockScreen()"):source.index(
            'document.querySelector("#logout-button")'
        )]

        self.assertIn("wakeWord?.stop", show_lock)
        self.assertIn("voiceSocket.close", show_lock)
        self.assertIn("releaseVoiceCapture", show_lock)
        self.assertIn("stopReplyAudio", show_lock)
        self.assertIn("stop,", hands_free[hands_free.index("return {"):])

    def test_multi_request_updates_plan_bar_and_evidence_lifecycle(self):
        source = APP_CLIENT.read_text(encoding="utf-8")
        markup = INDEX_PAGE.read_text(encoding="utf-8")
        render = source[source.index("function renderResult(result, elapsedMs)"):source.index("async function loadState()")]

        evidence = render.index("renderEvidenceLifecycle(result);")
        observe = render.index("multiRequest.observe(result);")
        plan_bar = render.index("renderPlanBar();")
        self.assertLess(evidence, observe)
        self.assertLess(observe, plan_bar)

        ask = markup[markup.index('id="ask-view"'):markup.index('id="activity-view"')]
        evidence_view = markup[markup.index('id="evidence-view"'):]
        self.assertIn('id="plan-bar"', ask)
        self.assertNotIn('id="evidence-diagnostics"', ask)
        self.assertIn('id="evidence-diagnostics"', evidence_view)

    def test_transaction_display_helpers_use_safe_currency_and_source_formats(self):
        script = f"""
          import {{formatTransactionalMoney, formatFundingSource}} from {json.dumps(TRANSACTION_DISPLAY_MODULE.as_uri())};
          process.stdout.write(JSON.stringify({{
            sgd: formatTransactionalMoney("SGD", 5000),
            usd: formatTransactionalMoney("USD", 5000),
            unknown: formatTransactionalMoney("EUR", 5000),
            zero: formatTransactionalMoney("SGD", 0),
            negative: formatTransactionalMoney("SGD", -5000),
            missing: formatTransactionalMoney("SGD", null),
            invalid: formatTransactionalMoney("SGD", "not-a-number"),
            source: formatFundingSource({{source_label: "Savings", source_last4: "1842"}}),
            accountSource: formatFundingSource({{source_label: "Savings", account_last4: "1842"}}),
            nicknameOnly: formatFundingSource({{source_label: "Savings"}}),
            noNickname: formatFundingSource({{source_last4: "1842"}}),
            savingsId: formatFundingSource({{funding_account_id: "acct_savings_01"}}),
            htmlLike: formatFundingSource({{source_label: "<Savings>", source_last4: "1842"}}),
          }}));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        values = json.loads(completed.stdout)
        self.assertEqual(values["sgd"], "S$50.00")
        self.assertEqual(values["usd"], "US$50.00")
        self.assertEqual(values["unknown"], "EUR 50.00")
        self.assertEqual(values["zero"], "S$0.00")
        self.assertEqual(values["negative"], "S$-50.00")
        self.assertEqual(values["missing"], "S$—")
        self.assertEqual(values["invalid"], "S$—")
        self.assertEqual(values["source"], "Savings · •••• 1842")
        self.assertEqual(values["accountSource"], "Savings · •••• 1842")
        self.assertEqual(values["nicknameOnly"], "Savings")
        self.assertEqual(values["noNickname"], "Selected account · •••• 1842")
        self.assertEqual(values["savingsId"], "Savings")
        self.assertEqual(values["htmlLike"], "<Savings> · •••• 1842")

    def test_transactional_review_surfaces_reference_shared_display_helpers(self):
        app_source = APP_CLIENT.read_text(encoding="utf-8")
        multi_source = (PROJECT_ROOT / "apps" / "dimon_live" / "static" / "multi_action_review.mjs").read_text(encoding="utf-8")
        paper_source = (PROJECT_ROOT / "apps" / "dimon_live" / "static" / "paper_equity_ui.mjs").read_text(encoding="utf-8")
        invest_source = INVEST_UI_MODULE.read_text(encoding="utf-8")

        for source in (app_source, multi_source, paper_source, invest_source):
            self.assertIn("transaction_display.mjs", source)
            self.assertIn("formatTransactionalMoney", source)
        self.assertIn("formatFundingSource", app_source)
        self.assertIn("formatFundingSource", multi_source)
        self.assertNotIn('style: "currency", currency: "SGD"', app_source)

    def test_locked_facts_and_approval_review_use_masked_source_and_exact_currency(self):
        source = APP_CLIENT.read_text(encoding="utf-8")
        self.assertIn("formatFundingSource(facts)", source)
        self.assertIn("formatFundingSource(review)", source)
        self.assertIn("formatTransactionalMoney(review.currency", source)
        self.assertIn("formatTransactionalMoney(item.currency", source)

    def test_reply_audio_error_copy_does_not_expose_internal_speech_provider(self):
        source = APP_CLIENT.read_text(encoding="utf-8")

        self.assertIn("I couldn’t play Dimon’s last reply", source)
        self.assertNotIn("Kokoro could not play Dimon’s last reply", source)

    def test_open_evidence_prioritizes_receipt_validation_trace_then_heading(self):
        script = f"""
          import {{openEvidence}} from {json.dumps(BANKING_SHELL_MODULE.as_uri())};
          const focused = [];
          const node = (name, receipt = null) => ({{
            dataset: receipt ? {{evidenceReceipt: receipt}} : {{}},
            hidden: true,
            classList: {{add() {{}}, remove() {{}}}},
            setAttribute() {{}},
            removeAttribute() {{}},
            focus() {{ focused.push(name); }},
          }});
          const view = node("view");
          const receipt = node("receipt", "receipt-1");
          const validation = node("validation");
          const trace = node("trace");
          const heading = node("heading");
          const root = {{
            querySelector(selector) {{
              return {{"#evidence-view": view, "#evidence-title": heading, "#evidence-validation": validation, "#trace-list": trace}}[selector] || null;
            }},
            querySelectorAll(selector) {{
              if (selector === "[data-view]") return [view];
              if (selector === "[data-view-target]") return [];
              if (selector === "[data-evidence-receipt]") return [receipt];
              if (selector === ".evidence-focus") return [];
              return [];
            }},
          }};
          const first = openEvidence(root, {{receiptId: "receipt-1", outcome: "EXECUTED_SYNTHETIC_TRANSFER"}});
          const second = openEvidence(root, {{receiptId: "missing", outcome: "BLOCKED", focusTarget: "validation"}});
          const third = openEvidence(root, {{outcome: "BLOCKED", focusTarget: "trace"}});
          const emptyRoot = {{
            querySelector(selector) {{
              return {{"#evidence-view": view, "#evidence-title": heading}}[selector] || null;
            }},
            querySelectorAll(selector) {{
              if (selector === "[data-view]") return [view];
              if (selector === "[data-view-target]") return [];
              if (selector === ".evidence-focus") return [];
              return [];
            }},
          }};
          const fourth = openEvidence(emptyRoot, {{outcome: "BLOCKED"}});
          process.stdout.write(JSON.stringify({{first, second, third, fourth, focused}}));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        values = json.loads(completed.stdout)
        self.assertEqual(values["focused"], ["receipt", "validation", "trace", "heading"])
        self.assertTrue(values["first"] and values["second"] and values["third"] and values["fourth"])

    def test_outcomes_and_evidence_view_expose_contextual_lifecycle_contract(self):
        source = APP_CLIENT.read_text(encoding="utf-8")
        markup = INDEX_PAGE.read_text(encoding="utf-8")
        self.assertIn("data-view-execution-evidence", source)
        self.assertIn("openEvidence", source)
        self.assertIn("View execution evidence", source)
        self.assertNotIn("JSON.stringify(result)", source)
        self.assertNotIn("approval_payload", source)
        for heading in ("Decision trace", "Validation", "Approval receipt", "Settlement", "Exactly-once status"):
            self.assertIn(heading, markup)
        self.assertLess(markup.index("Validation"), markup.index("Approval receipt"))
        self.assertLess(markup.index("Exactly-once status"), markup.index("Decision trace"))
        self.assertLess(markup.index("Validation"), markup.index("Approval receipt"))
        self.assertLess(markup.index("Approval receipt"), markup.index("Settlement"))
        self.assertLess(markup.index("Settlement"), markup.index("Exactly-once status"))

    def test_blocked_outcome_without_proof_has_safe_evidence_fallback(self):
        source = APP_CLIENT.read_text(encoding="utf-8")
        self.assertIn("focusTarget: \"heading\"", source)
        self.assertIn("No receipt or trace is available", source)
        self.assertIn("Already settled", source)
        self.assertIn("Awaiting approval", source)


    def test_multi_action_review_renders_each_domain_item(self):
        script = """
          import {renderMultiActionReview} from './apps/dimon_live/static/multi_action_review.mjs';
          const html = renderMultiActionReview([
            {kind:'BILL_PAYMENT', status:'AWAITING_APPROVAL', result:{receipt_choices:[{receipt_id:'bill-r', recipient_display_name:'PUB Water', source_label:'savings', amount_minor:3280, destination_kind:'UTILITY_BILL'}]}},
            {kind:'BENEFICIARY_TRANSFER', status:'AWAITING_APPROVAL', result:{receipt_choices:[{receipt_id:'transfer-r', recipient_display_name:'Alicia Powell', source_label:'savings', amount_minor:4500}]}},
            {kind:'STOCK_INVESTMENT', status:'ANSWERED', result:{answer:'NVIDIA quote ready'}}
          ], value => value, value => `S$${(value / 100).toFixed(2)}`);
          console.log(html);
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=PROJECT_ROOT,
            check=True,
            text=True,
            capture_output=True,
        )
        for expected in ("Bill payment", "PUB Water", "Alicia Powell", "Stock purchase", "S$32.80", "data-multi-index=\"1\""):
            self.assertIn(expected, completed.stdout)

    def test_multi_action_review_shows_stock_order_and_approval_control(self):
        script = """
          import {renderMultiActionReview} from './apps/dimon_live/static/multi_action_review.mjs';
          console.log(renderMultiActionReview([
            {kind:'STOCK_INVESTMENT', status:'AWAITING_APPROVAL', result:{paper_order:{order_id:'order-1', quantity:2, funding_currency:'SGD', max_funding_debit_minor:61750, instrument:{name:'NVIDIA Corporation', quote_currency:'USD'}, quote_snapshot:{price_minor:24147}}}},
            {kind:'BILL_PAYMENT', status:'AWAITING_APPROVAL', result:{answer:'Bill review'}}
          ], value => value));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=PROJECT_ROOT,
            check=True,
            text=True,
            capture_output=True,
        )
        for expected in ("NVIDIA Corporation", "2", "US$241.47 each", "S$617.50", "Review stock order", 'data-child-type="paper_order"'):
            self.assertIn(expected, completed.stdout)

    def test_grouped_review_keeps_usd_quote_sgd_debit_and_masked_source_distinct(self):
        script = """
          import {renderMultiActionReview} from './apps/dimon_live/static/multi_action_review.mjs';
          console.log(renderMultiActionReview([
            {kind:'STOCK_INVESTMENT', status:'AWAITING_APPROVAL', result:{paper_order:{order_id:'order-1', quantity:1, funding_currency:'SGD', max_funding_debit_minor:5000, funding_account_label:'Savings', funding_account_last4:'1842', instrument:{name:'Example Corp', quote_currency:'USD'}, quote_snapshot:{price_minor:2500}}}},
            {kind:'BENEFICIARY_TRANSFER', status:'AWAITING_APPROVAL', result:{receipt_choices:[{receipt_id:'transfer-r', recipient_display_name:'Alicia Powell', source_label:'Savings', source_last4:'1842', amount_minor:5000, currency:'SGD'}]}},
          ], value => String(value)));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("US$25.00 each", completed.stdout)
        self.assertIn("S$50.00", completed.stdout)
        self.assertIn("Savings · •••• 1842", completed.stdout)
        self.assertEqual(completed.stdout.count("US$25.00 each"), 1)
        self.assertNotIn("undefined", completed.stdout.lower())

    def test_unresolved_multi_action_uses_group_clarification_not_payment_review(self):
        program = """
          import {multiActionClarificationPresentation} from './apps/dimon_live/static/multi_action_review.mjs';
          const answer = "Who should receive S$10, and did you mean NVIDIA? Nothing has been prepared.";
          process.stdout.write(JSON.stringify(multiActionClarificationPresentation({
            outcome: "CLARIFICATION_REQUEST",
            reason_code: "MULTI_ACTION_ITEM_UNRESOLVED",
            answer,
            multi_action_clarification: {
              resolved_items: [{kind: "BILL_PAYMENT", evidence: "pay my water bill"}],
              issues: [
                {field: "recipient", question: "Who should receive S$10?"},
                {field: "instrument", question: "Did you mean NVIDIA?", suggested_value: "NVIDIA"},
              ],
            },
          })));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT, capture_output=True, check=False, text=True, encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {
            "title": "Two details need clarification",
            "question": "Who should receive S$10, and did you mean NVIDIA? Nothing has been prepared.",
            "detail": "I understood the water bill. No payment or order has been prepared.",
            "checklist": [
                {"label": "Bill payment", "status": "Understood"},
                {"label": "Recipient", "status": "Needs clarification"},
                {"label": "Stock", "status": "Needs clarification"},
            ],
        })

    def test_blocked_multi_action_does_not_render_partial_cards(self):
        script = """
          import {renderMultiActionReview} from './apps/dimon_live/static/multi_action_review.mjs';
          console.log(renderMultiActionReview([{kind:'BILL_PAYMENT'}], value => value));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=PROJECT_ROOT,
            check=True,
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.stdout.strip(), "")

    def test_utility_bill_review_and_success_have_distinct_payment_copy(self):
        source = APP_CLIENT.read_text(encoding="utf-8")

        self.assertIn('choice.destination_kind === "UTILITY_BILL"', source)
        self.assertIn('billReceipt ? "Review bill payment"', source)
        self.assertIn('result.outcome === "EXECUTED_SYNTHETIC_BILL_PAYMENT"', source)
        self.assertIn('"Savings balance"', source)
        self.assertIn("No external biller or bank was contacted.", source)

    def test_home_composer_hands_one_request_to_the_existing_assistant_form(self):
        program = f"""
          import {{ submitFromComposer }} from {json.dumps(BANKING_SHELL_MODULE.as_uri())};
          const calls = [];
          const homeForm = {{reset() {{ calls.push("reset"); }}}};
          const homeInput = {{value: "  Send John Smith S$50  "}};
          const targetInput = {{value: ""}};
          const targetForm = {{requestSubmit() {{ calls.push("submit"); }}}};
          const submitted = submitFromComposer(homeForm, homeInput, {{
            targetInput,
            targetForm,
            activate: () => calls.push("activate"),
          }});
          const empty = submitFromComposer(homeForm, {{value: "   "}}, {{targetInput, targetForm}});
          process.stdout.write(JSON.stringify({{submitted, empty, value: targetInput.value, calls}}));
        """

        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "submitted": True,
                "empty": False,
                "value": "Send John Smith S$50",
                "calls": ["activate", "submit", "reset"],
            },
        )

    def test_home_voice_and_shortcuts_enter_the_single_assistant_flow(self):
        source = APP_CLIENT.read_text(encoding="utf-8")
        markup = INDEX_PAGE.read_text(encoding="utf-8")

        self.assertIn('id="assistant-transcript"', markup)
        self.assertIn("submitFromComposer", source)
        self.assertIn("ui.voiceButton.click()", source)
        self.assertIn("[data-prompt]", source)
        self.assertIn("activateView(document, \"ask\")", source)
        self.assertIn("classifyActionResult", source)
        self.assertIn("assistantCategory", source)

    def test_home_composer_does_not_replace_a_pending_exact_retry(self):
        program = f"""
          import {{ submitFromComposer }} from {json.dumps(BANKING_SHELL_MODULE.as_uri())};
          const calls = [];
          const targetInput = {{value: "Original exact request"}};
          const submitted = submitFromComposer({{reset() {{ calls.push("reset"); }}}}, {{value: "Different request"}}, {{
            targetInput,
            targetForm: {{requestSubmit() {{ calls.push("submit"); }}}},
            canSubmit: () => false,
            onBlocked: () => calls.push("blocked"),
          }});
          process.stdout.write(JSON.stringify({{submitted, value: targetInput.value, calls}}));
        """

        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {"submitted": False, "value": "Original exact request", "calls": ["blocked"]},
        )

    def test_more_investment_actions_open_the_assistant_before_running(self):
        source = APP_CLIENT.read_text(encoding="utf-8")

        self.assertRegex(
            source,
            r'activateView\(document, "ask"\);\s+runCommand\(button\.dataset\.say\);',
        )

    def test_unknown_assistant_results_are_visible_without_success_language(self):
        source = APP_CLIENT.read_text(encoding="utf-8")

        self.assertIn('category === "unknown"', source)
        self.assertIn("Request needs attention", source)
        self.assertIn("No action was assumed", source)

    def test_voice_failure_reenables_typed_composers(self):
        source = APP_CLIENT.read_text(encoding="utf-8")

        self.assertIn("setComposerDisabled(false)", source)
        self.assertIn("Microphone unavailable · use typed input", source)

    def test_banking_home_exposes_normal_tasks_and_demo_boundaries(self):
        markup = INDEX_PAGE.read_text(encoding="utf-8")

        self.assertIn('class="promo-banner"', markup)
        self.assertIn('id="home-total-balance"', markup)
        self.assertIn('id="home-account-list"', markup)
        self.assertIn('id="recent-activity-list"', markup)
        self.assertIn("Synthetic demo · no real money", markup)
        self.assertEqual(markup.count('class="shortcut-button"'), 6)
        self.assertIn('href="/presenter"', markup)
        self.assertIn('data-view-target="evidence"', markup)
        wealth = markup[markup.index('id="wealth-view"'):markup.index('id="evidence-view"')]
        self.assertNotIn('data-view-link="evidence"', wealth)
        self.assertNotIn('main class="workspace"', markup)

    def test_synthetic_boundary_is_persistent_with_scoped_topbar_actions(self):
        markup = INDEX_PAGE.read_text(encoding="utf-8")
        styles = APP_STYLES.read_text(encoding="utf-8")
        marker = "Synthetic demo · no real money"

        self.assertIn('class="synthetic-marker"', markup)
        self.assertEqual(markup.count(marker), 5)
        self.assertIn('.synthetic-marker', styles)
        topbar = markup[markup.index('<header class="bank-topbar">'):markup.index("</header>")]
        self.assertIn('class="topbar-end"', topbar)
        topbar_end = topbar[topbar.index('class="topbar-end"'):]
        self.assertLess(topbar_end.index('class="synthetic-marker"'), topbar_end.index('class="topbar-actions"'))
        self.assertRegex(styles, r"\.topbar-end\s*\{[^}]*grid-column:\s*3;[^}]*display:\s*flex;")
        self.assertRegex(styles, r"\.icon-button\s*\{[^}]*display:\s*grid;[^}]*width:\s*40px;[^}]*height:\s*40px;[^}]*border:\s*0;[^}]*border-radius:\s*50%;")
        self.assertRegex(styles, r"\.icon-button svg\s*\{[^}]*width:\s*22px;[^}]*height:\s*22px;")
        for dialog_id in ("kyc-dialog", "singpass-dialog", "profile-dialog", "approval-dialog"):
            dialog = markup[markup.index(f'id="{dialog_id}"'):]
            dialog = dialog[:dialog.index("</dialog>")]
            self.assertIn('class="approval-synthetic-marker"', dialog)
            self.assertIn(marker, dialog)
        self.assertNotIn('aria-label="Notifications"', markup)
        self.assertIn('id="topbar-bell"', markup)
        self.assertIn('aria-label="No Singpass authorisations waiting"', markup)
        self.assertIn('id="logout-button"', markup)
        self.assertIn('aria-label="Log out and lock Dimon"', markup)

    def test_topbar_brand_mark_shares_the_text_baseline_center(self):
        styles = APP_STYLES.read_text(encoding="utf-8")

        self.assertRegex(
            styles,
            r"\.bank-topbar\s+\.brand-prepare\s*\{[^}]*transform:\s*translateY\(0\);",
        )

    def test_ready_badge_is_anchored_to_visible_logo_with_separate_hit_target(self):
        styles = APP_STYLES.read_text(encoding="utf-8")

        self.assertRegex(styles, r"\.brand-prepare\s*\{[^}]*width:\s*32px;[^}]*height:\s*32px;")
        self.assertRegex(styles, r"\.brand-prepare::before\s*\{[^}]*inset:\s*-6px;")
        self.assertRegex(styles, r"\.brand-ready-dot\s*\{[^}]*right:\s*-3px;[^}]*bottom:\s*-3px;")

    def test_kyc_consent_uses_standard_checkbox_with_accessible_hit_target(self):
        markup = INDEX_PAGE.read_text(encoding="utf-8")
        styles = APP_STYLES.read_text(encoding="utf-8")

        consent = markup[markup.index('id="kyc-consent"'):markup.index("</label>", markup.index('id="kyc-consent"'))]
        self.assertIn('class="kyc-consent-control"', consent)
        self.assertRegex(styles, r"\.kyc-consent-control\s*\{[^}]*width:\s*44px;[^}]*height:\s*44px;")
        self.assertRegex(styles, r"\.kyc-consent-control input\s*\{[^}]*width:\s*18px;[^}]*height:\s*18px;")

    def test_transaction_confirmation_checkboxes_use_compact_controls_with_large_hit_targets(self):
        markup = INDEX_PAGE.read_text(encoding="utf-8")
        styles = APP_STYLES.read_text(encoding="utf-8")

        for checkbox_id in ("approval-unusual-check", "approval-voice-confirmation-check"):
            checkbox = markup.index(f'id="{checkbox_id}"')
            control = markup.rfind('class="approval-checkbox-control"', 0, checkbox)
            self.assertGreater(control, markup.rfind("<label", 0, checkbox))
        self.assertRegex(
            styles,
            r"\.approval-checkbox-control\s*\{[^}]*width:\s*44px;[^}]*height:\s*44px;",
        )
        self.assertRegex(
            styles,
            r"\.approval-checkbox-control input\s*\{[^}]*width:\s*18px;[^}]*height:\s*18px;[^}]*min-width:\s*18px;[^}]*min-height:\s*18px;",
        )

    def test_shared_confirmation_dialog_resets_stock_state_before_paynow_or_bill_review(self):
        source = APP_CLIENT.read_text(encoding="utf-8")

        self.assertIn('function resetApprovalDialog({totalLabel = "Total debit"} = {})', source)
        self.assertIn('resetApprovalDialog();\n  const review = challenge.review;', source)
        self.assertIn(
            'resetApprovalDialog({totalLabel: selling ? "Estimated proceeds" : "Maximum debit"});',
            source,
        )
        self.assertIn("ui.approvalVoice.hidden = true;", source)
        self.assertIn("ui.approvalVoiceCheck.checked = false;", source)

    def test_hands_free_screen_approval_explicitly_autoplays_stored_receipt_readback(self):
        hands_free = HANDS_FREE_MODULE.read_text(encoding="utf-8")
        app = APP_CLIENT.read_text(encoding="utf-8")

        self.assertIn(
            'hooks.screenApproval(collection, receipt, {autoPlayReadBack: true})',
            hands_free,
        )
        self.assertIn("function confirmOnDevice(challenge, signerKind, options = {})", app)
        self.assertIn(
            "if (readBack && (needsReadBack || options.autoPlayReadBack || lastRequestWasSpoken))",
            app,
        )
        self.assertIn("async function signedApproval(collection, receipt, submit, interruptedDetail, options = {})", app)
        self.assertIn("confirmOnDevice(issued.challenge, credential?.kind, options)", app)
        self.assertIn("screenApproval: (collection, receipt, options = {}) => signedApproval(", app)

    def test_screen_approved_stock_fill_hands_audio_to_hands_free_player(self):
        hands_free = HANDS_FREE_MODULE.read_text(encoding="utf-8")
        app = APP_CLIENT.read_text(encoding="utf-8")

        self.assertIn("async function approvePaperOrder(order, {announcementOwner = \"screen\"} = {})", app)
        self.assertIn(
            'approvePaperOrder: order => approvePaperOrder(order, {announcementOwner: "hands_free"})',
            app,
        )
        self.assertIn('announcementOwner === "screen" && result.status === "FILLED"', app)
        self.assertIn("await wakeWord?.announceResult(renderedResult", app)
        self.assertIn("announceResult: sayResult", hands_free)

    def test_identity_failure_copy_is_shared_by_screen_and_hands_free(self):
        app = APP_CLIENT.read_text(encoding="utf-8")
        hands_free = HANDS_FREE_MODULE.read_text(encoding="utf-8")
        expected = (
            "Singpass identity verification wasn’t completed, so the order could not be approved. "
            "Nothing was bought. Verify your identity and try again."
        )
        program = f"""
          import {{ IDENTITY_VERIFICATION_FAILURE_MESSAGE }} from {json.dumps(IDENTITY_FEEDBACK_MODULE.as_uri())};
          process.stdout.write(IDENTITY_VERIFICATION_FAILURE_MESSAGE);
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program], cwd=PROJECT_ROOT,
            capture_output=True, check=False, text=True, encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, expected)
        self.assertIn('from "./identity_feedback.mjs"', app)
        self.assertIn('from "./identity_feedback.mjs"', hands_free)
        self.assertNotIn("kyc not completed", app.lower())

    def test_safety_explainer_and_evidence_are_first_class_navigation(self):
        markup = INDEX_PAGE.read_text(encoding="utf-8")
        source = APP_CLIENT.read_text(encoding="utf-8")
        banner_start = markup.rfind("<button", 0, markup.index('class="promo-banner"'))
        banner = markup[banner_start:markup.index("</button>", banner_start) + len("</button>")]
        safety_start = markup.index('data-view="safety"')
        safety_end = markup.index('\n      </section>\n\n      <section id="evidence-view"', safety_start)
        safety = markup[safety_start:safety_end]
        primary_nav = markup[markup.index('class="bottom-navigation"'):]

        self.assertTrue("<button" in banner or "<a" in banner)
        self.assertIn('data-view-link="safety"', banner)
        self.assertIn('data-view="safety"', markup)
        self.assertIn("Understand → Lock exact details → Validate policy → Approve → Settle once → Prove with evidence", safety)
        self.assertEqual(safety.count("Open technical evidence"), 1)
        self.assertIn('data-view-target="evidence"', primary_nav)
        self.assertNotIn('data-view-target="activity"', primary_nav)
        self.assertIn('data-view-link="activity"', markup)
        self.assertNotIn('data-view-link="evidence"', markup[markup.index('id="wealth-view"'):markup.index('id="evidence-view"')])
        self.assertIn("viewNameFromHash", source)
        self.assertIn("hashchange", source)

    def test_view_hashes_resolve_to_stable_views_and_heading_focus(self):
        program = f"""
          import {{ activateView, viewNameFromHash }} from {json.dumps(BANKING_SHELL_MODULE.as_uri())};
          const heading = {{focusCalls: 0, focus() {{ this.focusCalls += 1; }}}};
          const views = [
            {{dataset: {{view: 'home'}}, hidden: false, querySelector(selector) {{ return selector.startsWith('h1') ? heading : null; }}, setAttribute() {{}}, removeAttribute() {{}}}},
            {{dataset: {{view: 'evidence'}}, hidden: true, querySelector(selector) {{ return selector.startsWith('h1') ? heading : null; }}, setAttribute() {{}}, removeAttribute() {{}}}},
          ];
          const root = {{
            querySelector(selector) {{
              if (selector === '#evidence-view') return views[1];
              if (selector === '#topbar-view-title') return null;
              return null;
            }},
            querySelectorAll(selector) {{
              if (selector === '[data-view]') return views;
              return [];
            }},
          }};
          const evidenceActivated = activateView(root, viewNameFromHash('#evidence'));
          process.stdout.write(JSON.stringify({{evidenceActivated, focusCalls: heading.focusCalls, safety: viewNameFromHash('#safety'), invalid: viewNameFromHash('#unknown')}}));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {"evidenceActivated": True, "focusCalls": 1, "safety": "safety", "invalid": None},
        )

    def test_home_dom_order_is_canonical_without_css_reordering(self):
        markup = INDEX_PAGE.read_text(encoding="utf-8")
        styles = APP_STYLES.read_text(encoding="utf-8")

        order = [
            markup.index('class="promo-banner"'),
            markup.index('id="home-title"'),
            markup.index('id="home-command-form"'),
            markup.index('class="balance-summary"'),
            markup.index('class="shortcut-section"'),
            markup.index('class="home-activity-preview"'),
            markup.index('class="bottom-navigation-clearance"'),
        ]
        self.assertEqual(order, sorted(order))
        self.assertNotRegex(styles, r"\.home-view[^{}]*\{[^{}]*\border\s*:")
        self.assertIn('class="bottom-navigation-clearance"', markup)

    def test_phase_four_uses_one_token_system_for_ui_colors(self):
        styles = APP_STYLES.read_text(encoding="utf-8")
        investing = (PROJECT_ROOT / "apps/dimon_live/static/invest_ui.mjs").read_text(encoding="utf-8")
        markup = INDEX_PAGE.read_text(encoding="utf-8")
        # Contrast audit: primary, body, muted, accent, warning, danger, info,
        # inverse, and focus pairs all measure at least 4.5:1 against their
        # semantic surfaces (the strongest checked pair is 17.60:1).
        for token in (
            "--surface-canvas", "--surface-panel", "--surface-raised", "--surface-inverse",
            "--text-strong", "--text-body", "--text-muted", "--text-on-accent",
            "--border-default", "--border-strong", "--accent", "--accent-hover", "--focus",
            "--status-success", "--status-warning", "--status-danger", "--status-info",
            "--chart-series-1", "--chart-series-2", "--chart-series-3",
        ):
            self.assertIn(token, styles)
        literal = re.compile(r"(?<![\w-])(?:#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)|hsla?\([^)]*\))")
        rootless_css = re.sub(r":root\s*\{[^{}]*\}", "", styles, flags=re.S)
        self.assertEqual(literal.findall(rootless_css), [])
        self.assertEqual(literal.findall(investing), [])
        self.assertEqual(literal.findall(markup), [])

    def test_phase_four_controls_have_shared_hit_area_and_focus_contract(self):
        styles = APP_STYLES.read_text(encoding="utf-8")
        self.assertRegex(styles, r"button\s*,\s*a\[href\].*min-height:\s*44px")
        self.assertIn("input[type=\"checkbox\"]", styles)
        self.assertIn("input[type=\"range\"]", styles)
        for control in (
            ".home-speak-button", ".home-type-toggle", ".composer-send",
            ".wealth-ask-button", ".approval-cancel", ".approval-confirm",
            ".bottom-navigation button",
        ):
            self.assertIn(control, styles)
        self.assertNotRegex(
            styles,
            r"(?:home-type-toggle|composer-send|wealth-ask-button|approval-(?:cancel|confirm)|bottom-navigation button)[^{}]*min-height:\s*(?:[0-3]?\d|4[0-3])px",
        )
        self.assertIn("summary:focus-visible", styles)
        self.assertIn("input:focus-visible", styles)
        self.assertIn("select:focus-visible", styles)
        self.assertIn(".evidence-focus", styles)
        self.assertRegex(styles, r"@media \(prefers-reduced-motion: reduce\)[\s\S]*evidence-focus")

    def test_approval_facts_wrap_at_narrow_widths(self):
        styles = APP_STYLES.read_text(encoding="utf-8")
        self.assertRegex(styles, r"\.approval-facts div \{[^}]*flex-wrap:\s*wrap")
        self.assertRegex(styles, r"\.approval-facts dd \{[^}]*min-width:\s*0")
        self.assertIn("@media (max-width: 520px)", styles)

    def test_ask_starts_as_a_conversation_and_hides_diagnostics_in_evidence(self):
        markup = INDEX_PAGE.read_text(encoding="utf-8")
        ask_start = markup.index('id="ask-view"')
        evidence_start = markup.index('id="evidence-view"')
        ask = markup[ask_start:evidence_start]
        evidence = markup[evidence_start:]

        self.assertIn('id="voice-status" class="sr-only" role="status" aria-live="polite"', ask)
        self.assertNotIn('id="intent-history"', ask)
        self.assertIn('id="intent-history"', evidence)
        self.assertIn('id="outcome-panel" class="outcome-panel waiting" aria-live="off" hidden', ask)

    def test_shortcut_icons_are_visually_prominent_on_mobile(self):
        styles = APP_STYLES.read_text(encoding="utf-8")

        self.assertIn(".shortcut-button svg { width: 30px; height: 30px;", styles)
        self.assertIn(".shortcut-button {", styles)
        self.assertIn("min-height: 88px", styles)

    def test_ask_is_a_persistent_conversation_with_existing_voice_service(self):
        markup = INDEX_PAGE.read_text(encoding="utf-8")
        styles = APP_STYLES.read_text(encoding="utf-8")
        source = APP_CLIENT.read_text(encoding="utf-8")
        ask_start = markup.index('id="ask-view"')
        ask_end = markup.index('id="activity-view"')
        ask = markup[ask_start:ask_end]

        self.assertIn('class="agent-identity"', ask)
        self.assertIn("Ready when you are", ask)
        self.assertIn('id="assistant-transcript"', ask)
        self.assertIn('id="voice-button" class="voice-primary-button"', ask)
        self.assertNotIn("voice-orb", ask)
        self.assertNotIn('id="transcript-disclosure"', ask)
        self.assertEqual(ask.count('data-suggestion-intent='), 3)
        self.assertIn('id="outcome-panel"', ask)
        self.assertLess(ask.index('id="assistant-transcript"'), ask.index('id="outcome-panel"'))
        self.assertIn('id="command-form" class="voice-text-fallback conversation-composer"', ask)
        self.assertIn('rows="1"', ask)
        self.assertIn('placeholder="What would you like to do?"', ask)
        self.assertNotIn(' required', ask[ask.index('id="command-input"'):ask.index('</form>')])
        self.assertIn('id="conversation-status"', ask)
        self.assertIn('role="status" aria-live="polite"', ask)
        self.assertIn('dataset.voiceState', source)
        self.assertIn("setAssistantMode(\"understanding\")", source)
        self.assertIn("scrollIntoView", source)
        self.assertIn("autoGrowComposer", source)
        self.assertIn(".ask-view:not([hidden])", styles)
        self.assertIn(".conversation-composer {", styles)
        self.assertIn("scroll-padding-block", styles)

    def test_identity_setup_is_compact_and_voice_controls_are_progressively_disclosed(self):
        markup = INDEX_PAGE.read_text(encoding="utf-8")
        styles = APP_STYLES.read_text(encoding="utf-8")
        ask = markup[markup.index('id="ask-view"'):markup.index('id="activity-view"')]

        self.assertIn('class="identity-setup-row"', ask)
        self.assertIn('id="identity-status-label"', ask)
        self.assertIn('id="verify-identity-note" class="hands-free-note" aria-live="polite" hidden', ask)
        self.assertIn('<details class="voice-settings">', ask)
        self.assertIn('<summary><span>Voice settings</span>', ask)
        self.assertLess(ask.index('id="verify-identity-button"'), ask.index('<details class="voice-settings">'))
        self.assertIn(".identity-setup-row {", styles)
        self.assertIn(".voice-settings > summary {", styles)

    def test_active_conversation_uses_compact_voice_bar(self):
        markup = INDEX_PAGE.read_text(encoding="utf-8")
        styles = APP_STYLES.read_text(encoding="utf-8")
        source = APP_CLIENT.read_text(encoding="utf-8")

        self.assertIn('ui.askView?.classList.add("conversation-started")', source)
        ask = markup[markup.index('id="ask-view"'):markup.index('id="activity-view"')]
        scroll_start = ask.index('class="conversation-scroll-region"')
        self.assertLess(scroll_start, ask.index('id="hands-free-panel"'))
        self.assertLess(scroll_start, ask.index('id="assistant-transcript"'))
        self.assertIn(".ask-view.conversation-started .voice-presence {", styles)
        self.assertRegex(
            styles,
            r"\.conversation-scroll-region \{[^}]*overflow-y:\s*auto",
        )
        self.assertRegex(
            styles,
            r"\.ask-view\.conversation-started \.voice-waveform \{[^}]*display:\s*none",
        )
        self.assertRegex(
            styles,
            r"\.ask-view\.conversation-started \.voice-primary-button \{[^}]*min-height:\s*48px",
        )

    def test_voice_identity_status_copy_is_server_appropriate(self):
        source = HANDS_FREE_MODULE.read_text(encoding="utf-8")

        self.assertIn("Voice identity ready", source)
        self.assertIn("Voice identity unavailable", source)
        self.assertIn("Checking voice identity…", source)
        self.assertNotIn("install.cmd", source)

    def test_starter_suggestions_use_the_existing_assistant_action_flow(self):
        program = f"""
          import {{ resolveStarterIntent }} from {json.dumps(BANKING_SHELL_MODULE.as_uri())};
          process.stdout.write(JSON.stringify([
            resolveStarterIntent("AFFORDABILITY"),
            resolveStarterIntent("PAYMENT"),
            resolveStarterIntent("INVESTMENTS"),
            resolveStarterIntent("UNEXPECTED"),
          ]));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            ["affordability", "payment", "investments", None],
        )

        markup = INDEX_PAGE.read_text(encoding="utf-8")
        source = APP_CLIENT.read_text(encoding="utf-8")
        self.assertIn('data-suggestion-intent="AFFORDABILITY"', markup)
        self.assertIn('data-suggestion-intent="PAYMENT"', markup)
        self.assertIn('data-suggestion-intent="INVESTMENTS"', markup)
        self.assertIn('data-suggestion-prompt="I want to pay someone"', markup)
        self.assertIn('if (intent) runCommand(button.dataset.suggestionPrompt || button.textContent.trim())', source)
        self.assertIn('speakReply(result, {autoListen: speakAutomatically && !pendingOnScreenReview})', source)
        self.assertIn("ui.suggestionList.hidden = true", source)
        self.assertIn(".voice-examples[hidden] { display: none !important; }", APP_STYLES.read_text(encoding="utf-8"))

    def test_normal_ask_clarifications_do_not_expose_model_pipeline_copy(self):
        source = APP_CLIENT.read_text(encoding="utf-8")
        invest_ui = (PROJECT_ROOT / "apps" / "dimon_live" / "static" / "invest_ui.mjs").read_text(encoding="utf-8")

        self.assertNotIn("The AI only drafted this", source)
        self.assertNotIn("Qwen will not run again", source)
        self.assertNotIn("Qwen will not interpret", source)
        self.assertNotIn("built from your words, not by the AI", invest_ui)
        self.assertIn('result.reason_code === "MISSING_RECIPIENT"', source)
        self.assertIn("Who would you like to pay?", source)
        self.assertIn("Nothing has been sent.", source)

    def test_ask_keeps_text_and_voice_composers_visible_together(self):
        markup = INDEX_PAGE.read_text(encoding="utf-8")
        styles = APP_STYLES.read_text(encoding="utf-8")
        source = APP_CLIENT.read_text(encoding="utf-8")
        ask = markup[markup.index('id="ask-view"'):markup.index('id="activity-view"')]

        self.assertIn('data-voice-state="idle"', ask)
        self.assertIn('id="voice-button"', ask)
        self.assertNotIn('class="voice-orb"', ask)
        self.assertIn("Ready when you are", ask)
        self.assertIn('id="voice-stage-status" class="voice-stage-status" role="status" aria-live="polite"', ask)
        self.assertNotIn('<details id="transcript-disclosure"', ask)
        self.assertIn('id="voice-text-toggle" class="voice-text-toggle"', ask)
        self.assertIn('aria-controls="command-form"', ask)
        self.assertIn('id="command-form" class="voice-text-fallback conversation-composer" novalidate hidden', ask)
        self.assertNotIn('id="voice-speech-controls"', ask)
        self.assertNotIn('id="command-input" name="command" maxlength="500" rows="1" autocomplete="off" autocapitalize="sentences" spellcheck="true" placeholder="Message Dimon…" aria-label="Message Dimon" required', ask)
        self.assertNotIn("SpeechRecognition", source)
        self.assertNotIn("speechSynthesis", source)
        self.assertIn("startAudioWorkletVoice", source)
        self.assertIn("waitForAudioArtifact", source)
        self.assertIn("autoplayResultAudio", source)
        self.assertNotIn(".voice-orb {", styles)
        self.assertIn('item.className = `transcript-turn', source)

    def test_agent_speech_uses_server_audio_without_browser_speech_apis(self):
        source = APP_CLIENT.read_text(encoding="utf-8")
        self.assertIn("/api/voice/session", source)
        self.assertIn("/api/audio/${encodeURIComponent(artifactId)}", source)
        self.assertIn("await autoplayResultAudio(audio, {onEnded: finish});", source)
        self.assertNotIn("SpeechRecognition", source)
        self.assertNotIn("speechSynthesis", source)

    def test_transaction_proposal_has_edit_and_review_without_eager_approval_controls(self):
        source = APP_CLIENT.read_text(encoding="utf-8")

        self.assertIn('class="candidate-edit"', source)
        self.assertIn('class="candidate-choice" data-index="${index}"', source)
        self.assertIn("choice.validation", source)
        self.assertIn("aria-label=\"Edit request\"", source)
        self.assertIn('id="approval-confirm"', INDEX_PAGE.read_text(encoding="utf-8"))

    def test_banking_shell_css_handles_safe_areas_wrapping_focus_and_motion(self):
        styles = APP_STYLES.read_text(encoding="utf-8")

        self.assertIn("env(safe-area-inset-bottom)", styles)
        self.assertIn("@media (prefers-reduced-motion: reduce)", styles)
        self.assertIn(":focus-visible", styles)
        self.assertIn("overflow-wrap: anywhere", styles)
        self.assertIn(".bottom-navigation", styles)
        self.assertIn("#17191d", styles.lower())
        self.assertIn("#c9152c", styles.lower())
        self.assertIn(".home-composer:focus-within", styles)
        self.assertIn("#run-button:hover", styles)
        self.assertIn(".bank-account-card > div { min-width: 0;", styles)
        self.assertIn(".bank-account-type, .bank-account-number", styles)
        self.assertIn(".scenario-card code {", styles)
        self.assertIn("color: var(--paper);", styles)

    def test_banking_shell_renders_owned_accounts_and_consumer_activity(self):
        program = f"""
          import {{ renderHomeSummary }} from {json.dumps(BANKING_SHELL_MODULE.as_uri())};
          const result = renderHomeSummary({{
            accounts: [
              {{account_id: "owned", label: "My <Savings>", balance_minor: 123450, is_user_source: 1}},
              {{account_id: "payee", label: "Alicia", balance_minor: 9000, is_user_source: 0}},
            ],
            transactions: [{{
              source_label: "savings",
              destination_label: "Alicia <Powell>",
              amount_minor: 250,
              transaction_id: "txn-secret-full-identifier",
            }}],
          }}, {{
            escapeHtml: value => String(value).replaceAll("<", "&lt;").replaceAll(">", "&gt;"),
            formatSgd: minor => `S$${{(minor / 100).toFixed(2)}}`,
          }});
          process.stdout.write(JSON.stringify(result));
        """

        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["totalMinor"], 123450)
        self.assertIn("My &lt;Savings&gt;", result["accountsHtml"])
        self.assertNotIn("Alicia</p>", result["accountsHtml"])
        self.assertIn("Alicia &lt;Powell&gt;", result["activityHtml"])
        self.assertIn("S$2.50", result["activityHtml"])
        self.assertNotIn("txn-secret-full-identifier", result["activityHtml"])

    def test_banking_shell_renders_helpful_empty_states(self):
        program = f"""
          import {{ renderHomeSummary }} from {json.dumps(BANKING_SHELL_MODULE.as_uri())};
          const result = renderHomeSummary({{}}, {{
            escapeHtml: String,
            formatSgd: minor => `S$${{(minor / 100).toFixed(2)}}`,
          }});
          process.stdout.write(JSON.stringify(result));
        """

        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["totalMinor"], 0)
        self.assertIn("No accounts are available", result["accountsHtml"])
        self.assertIn("No recent activity", result["activityHtml"])

    def test_banking_shell_classifies_known_and_unknown_results(self):
        program = f"""
          import {{ classifyActionResult }} from {json.dumps(BANKING_SHELL_MODULE.as_uri())};
          const cases = [
            {{reason_code: "SIGNED_APPROVAL_REQUIRED", receipt: {{}}}},
            {{reason_code: "REAL_STOCK_BRIEF", investing: {{view: "REAL_STOCK_BRIEF"}}}},
            {{reason_code: "BALANCE_INQUIRY"}},
            {{reason_code: "SERVICE_REQUEST", intent_type: "SERVICE_REQUEST"}},
            {{reason_code: "MONEY_REQUEST_SENT", money_request: {{}}}},
            {{reason_code: "REAL_STOCK_RESEARCH", investing: {{view: "REAL_STOCK_RESEARCH"}}}},
            {{reason_code: "SOMETHING_NEW"}},
          ];
          process.stdout.write(JSON.stringify(cases.map(classifyActionResult)));
        """

        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            ["payments", "investing", "account", "request", "request", "unknown", "unknown"],
        )

    def test_unknown_answered_results_are_guarded_before_success_rendering(self):
        source = APP_CLIENT.read_text(encoding="utf-8")

        guard = source.index('result.outcome === "ANSWERED" && category === "unknown"')
        generic_success = source.index('result.outcome === "ANSWERED")', guard + 1)
        self.assertLess(guard, generic_success)
        self.assertIn("Request needs attention", source[guard:generic_success])

    def test_banking_shell_activates_one_known_view_and_rejects_unknown_view(self):
        program = f"""
          import {{ activateView }} from {json.dumps(BANKING_SHELL_MODULE.as_uri())};
          const heading = {{focusCalls: 0, focus() {{ this.focusCalls += 1; }}}};
          const views = [
            {{dataset: {{view: "home"}}, id: "home-view", hidden: false, hiddenAttribute: false, setAttribute(name) {{ if (name === "hidden") this.hiddenAttribute = true; }}, removeAttribute(name) {{ if (name === "hidden") this.hiddenAttribute = false; }}, querySelector: () => null}},
            {{dataset: {{view: "ask"}}, id: "ask-view", hidden: true, hiddenAttribute: true, setAttribute(name) {{ if (name === "hidden") this.hiddenAttribute = true; }}, removeAttribute(name) {{ if (name === "hidden") this.hiddenAttribute = false; }}, querySelector: () => heading}},
          ];
          const buttons = ["home", "ask"].map(name => ({{
            dataset: {{viewTarget: name}},
            current: null,
            setAttribute(name, value) {{ if (name === "aria-current") this.current = value; }},
            removeAttribute(name) {{ if (name === "aria-current") this.current = null; }},
          }}));
          const root = {{
            querySelector: selector => views.find(view => selector.includes(view.dataset.view)) || null,
            querySelectorAll: selector => selector.includes("data-view-target") ? buttons : views,
          }};
          const activated = activateView(root, "ask");
          const unknown = activateView(root, "missing");
          process.stdout.write(JSON.stringify({{
            activated,
            unknown,
            hidden: views.map(view => view.hidden),
            hiddenAttributes: views.map(view => view.hiddenAttribute),
            current: buttons.map(button => button.current),
            focusCalls: heading.focusCalls,
          }}));
        """

        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "activated": True,
                "unknown": False,
            "hidden": [True, False],
            "hiddenAttributes": [True, False],
                "current": [None, "page"],
                "focusCalls": 1,
            },
        )

    def test_banking_home_shell_has_safety_and_evidence_views_with_persistent_ask_entry(self):
        markup = INDEX_PAGE.read_text(encoding="utf-8")

        for view_id in (
            "home-view",
            "pay-view",
            "ask-view",
            "activity-view",
            "safety-view",
            "evidence-view",
        ):
            self.assertIn(f'id="{view_id}"', markup)
        self.assertIn('id="home-command-form"', markup)
        self.assertIn('id="command-form"', markup)
        self.assertEqual(markup.count("data-view-target="), 6)

    def test_technical_panels_live_only_inside_evidence_view(self):
        markup = INDEX_PAGE.read_text(encoding="utf-8")
        evidence_start = markup.index('id="evidence-view"')

        for technical_id in (
            'id="inference-mode-badge"',
            'id="trace-list"',
            'id="commit-panel"',
            'id="ledger-body"',
        ):
            self.assertGreater(markup.index(technical_id), evidence_start)

    def test_navigation_uses_svg_line_icons_without_unicode_control_icons(self):
        markup = INDEX_PAGE.read_text(encoding="utf-8")
        navigation_start = markup.index('aria-label="Primary navigation"')
        navigation = markup[navigation_start:]

        self.assertEqual(navigation.count("data-view-target="), 5)
        self.assertGreaterEqual(navigation.count("<svg"), 5)
        self.assertGreaterEqual(navigation.count('fill="none"'), 5)
        self.assertNotIn("●", navigation)
        self.assertNotIn("↵", navigation)

    def test_interactive_controls_use_svg_instead_of_unicode_pictograms(self):
        markup = INDEX_PAGE.read_text(encoding="utf-8")

        self.assertNotIn("▶ Play again", markup)
        self.assertIn('id="approval-readback-play"', markup)
        self.assertIn('class="control-icon"', markup)

    def test_wealth_catalog_filter_is_case_insensitive_composable_and_stable(self):
        script = f"""
          import {{filterWealthCatalog}} from {json.dumps(INVEST_UI_MODULE.as_uri())};
          const items = [
            {{symbol: "ES3", name: "State Street ETF", kind: "ETF", sector: "Singapore equities"}},
            {{symbol: "VOO", name: "Vanguard S&P 500 ETF", kind: "ETF", sector: "U.S. large-cap equities"}},
            {{symbol: "AAPL", name: "Apple Inc.", kind: "STOCK", sector: "Technology"}},
            {{symbol: "MSFT", name: "Microsoft Corporation", kind: "STOCK", sector: "Technology"}},
          ];
          const result = {{
            queryName: filterWealthCatalog(items, {{query: " vAnGuArD "}}).map(item => item.symbol),
            querySymbol: filterWealthCatalog(items, {{query: " aApL "}}).map(item => item.symbol),
            funds: filterWealthCatalog(items, {{kind: "Funds & ETFs"}}).map(item => item.symbol),
            equities: filterWealthCatalog(items, {{kind: "Equities"}}).map(item => item.symbol),
            combined: filterWealthCatalog(items, {{query: "tech", kind: "Equities"}}).map(item => item.symbol),
            sector: filterWealthCatalog(items, {{sector: "technology"}}).map(item => item.symbol),
            stable: filterWealthCatalog(items, {{query: "a"}}).map(item => item.symbol),
            none: filterWealthCatalog(items, {{query: "no match"}}),
          }};
          process.stdout.write(JSON.stringify(result));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "queryName": ["VOO"],
                "querySymbol": ["AAPL"],
                "funds": ["ES3", "VOO"],
                "equities": ["AAPL", "MSFT"],
                "combined": [],
                "sector": ["AAPL", "MSFT"],
                "stable": ["ES3", "VOO", "AAPL", "MSFT"],
                "none": [],
            },
        )

    def test_wealth_has_accessible_overview_explore_and_plans_sections(self):
        markup = INDEX_PAGE.read_text(encoding="utf-8")
        wealth_start = markup.index('id="wealth-view"')
        wealth_end = markup.index('\n      </section>\n\n      <section id="safety-view"', wealth_start)
        wealth = markup[wealth_start:wealth_end]

        for panel_id in ("wealth-overview", "wealth-explore", "wealth-plans"):
            self.assertIn(f'id="{panel_id}"', wealth)
            self.assertIn(f'aria-labelledby="{panel_id}-title"', wealth)
        self.assertEqual(wealth.count('role="tab"'), 3)
        self.assertIn('aria-selected="true"', wealth)
        self.assertIn('id="wealth-explore-search"', wealth)
        self.assertIn('id="wealth-explore-kind"', wealth)
        self.assertIn('id="wealth-explore-count"', wealth)
        self.assertIn('id="wealth-explore-results"', wealth)
        self.assertIn('id="wealth-investor-profile"', wealth)
        self.assertIn('id="wealth-starter"', wealth)
        self.assertIn("Goals", wealth)
        self.assertIn("recurring monthly plan", wealth)
        self.assertNotIn('href="/presenter"', wealth)
        self.assertNotIn('data-view-target="evidence"', wealth)

    def test_wealth_catalog_uses_compact_expandable_rows_and_safe_issuer_links(self):
        script = f"""
          import {{renderWealthCatalog}} from {json.dumps(INVEST_UI_MODULE.as_uri())};
          const html = renderWealthCatalog([{{
            symbol: "VOO", name: "Vanguard S&P 500 ETF", kind: "ETF",
            sector: "U.S. large-cap equities", factLabel: "Tracks",
            fact: "S&P 500 Index", summary: "Broad exposure", sourceName: "Vanguard",
            sourceUrl: "https://example.test/issuer", askPrompt: "Explain VOO",
          }}], {{escapeHtml: value => String(value)}});
          process.stdout.write(html);
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('class="wealth-explore-row"', completed.stdout)
        self.assertIn("<details>", completed.stdout)
        self.assertIn('target="_blank" rel="noopener noreferrer"', completed.stdout)
        self.assertIn('data-say="Explain VOO"', completed.stdout)
        self.assertNotIn("wealth-catalog-card", completed.stdout)
        self.assertIn("No matching instruments", self._render_empty_catalog())

    def _render_empty_catalog(self):
        script = f"""
          import {{renderWealthCatalog}} from {json.dumps(INVEST_UI_MODULE.as_uri())};
          process.stdout.write(renderWealthCatalog([], {{escapeHtml: value => String(value)}}));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8",
        )
        return completed.stdout

    def test_wealth_navigation_supports_keyboard_tabs_and_safe_empty_states(self):
        source = APP_CLIENT.read_text(encoding="utf-8")
        invest_source = INVEST_UI_MODULE.read_text(encoding="utf-8")
        markup = INDEX_PAGE.read_text(encoding="utf-8")

        for key in ("ArrowRight", "ArrowLeft", "Home", "End"):
            self.assertIn(key, source)
        self.assertIn("wealthSection", source)
        self.assertIn("aria-live=\"polite\"", markup)
        self.assertIn("No matching instruments", invest_source)
        self.assertIn("Explore is temporarily unavailable", source)
        self.assertIn("wealth-plans", markup)
        self.assertIn("wealth-overview", markup)

    def test_existing_approval_dialog_coordinates_remain_present(self):
        markup = INDEX_PAGE.read_text(encoding="utf-8")

        for element_id in (
            "approval-dialog",
            "approval-source",
            "approval-total",
            "approval-digest",
            "approval-method",
            "approval-confirm",
            "approval-cancel",
        ):
            self.assertIn(f'id="{element_id}"', markup)

    def test_action_card_is_part_of_the_persistent_conversation(self):
        markup = INDEX_PAGE.read_text(encoding="utf-8")
        assistant_start = markup.index('id="ask-view"')
        result = markup.index('id="outcome-panel"', assistant_start)
        transcript = markup.index('id="assistant-transcript"', assistant_start)
        self.assertLess(transcript, result)
        self.assertIn('id="command-form" class="voice-text-fallback conversation-composer"', markup[assistant_start:])

    def test_stock_brief_client_supports_conversational_followups(self):
        source = APP_CLIENT.read_text(encoding="utf-8")
        self.assertIn("/api/conversations/", source)
        self.assertIn("expected_version", source)
        self.assertIn("conversation_id", source)
        self.assertIn("USE_AVAILABLE_FUNDS", source)
        self.assertIn("CHANGE_FUNDING_ACCOUNT", source)
        self.assertIn("new URLSearchParams", source)
        self.assertIn("conversation_id:", source)

    def test_result_audio_reuses_successful_poll_response_and_loads_metadata(self):
        program = f"""
          import {{ waitForAudioArtifact }} from {json.dumps(RESULT_AUDIO_MODULE.as_uri())};
          const calls = [];
          const audio = {{
            dataset: {{}},
            hidden: true,
            preload: "none",
            src: "",
            load() {{ calls.push("load"); }},
          }};
          const responses = [
            {{
              status: 202,
              ok: true,
              blob: async () => new Blob([], {{type: ""}}),
            }},
            {{
              status: 200,
              ok: true,
              blob: async () => new Blob(["RIFF-test-WAVE"], {{type: "audio/wav"}}),
            }},
          ];
          let fetchCount = 0;
          const result = await waitForAudioArtifact(audio, "/api/audio/artifact-1", {{
            fetchImpl: async () => {{ fetchCount += 1; return responses.shift(); }},
            sleep: async () => {{}},
            createObjectUrl: blob => {{ calls.push(`blob:${{blob.type}}:${{blob.size}}`); return "blob:stock-brief"; }},
          }});
          process.stdout.write(JSON.stringify({{
            result,
            fetchCount,
            src: audio.src,
            preload: audio.preload,
            hidden: audio.hidden,
            calls,
          }}));
        """

        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {
            "result": "blob:stock-brief",
            "fetchCount": 2,
            "src": "blob:stock-brief",
            "preload": "metadata",
            "hidden": False,
            "calls": ["blob:audio/wav:14", "load"],
        })

    def test_result_audio_releases_the_previous_blob_before_a_new_result(self):
        program = f"""
          import {{ clearResultAudio }} from {json.dumps(RESULT_AUDIO_MODULE.as_uri())};
          const calls = [];
          const audio = {{
            dataset: {{resultAudioObjectUrl: "blob:old-stock-brief"}},
            hidden: false,
            src: "blob:old-stock-brief",
            pause() {{ calls.push("pause"); }},
            removeAttribute(name) {{ calls.push(`remove:${{name}}`); this.src = ""; }},
            load() {{ calls.push("load"); }},
          }};
          clearResultAudio(audio, {{revokeObjectUrl: url => calls.push(`revoke:${{url}}`)}});
          process.stdout.write(JSON.stringify({{
            hidden: audio.hidden,
            src: audio.src,
            storedUrl: audio.dataset.resultAudioObjectUrl || null,
            calls,
          }}));
        """

        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {
            "hidden": True,
            "src": "",
            "storedUrl": None,
            "calls": [
                "pause",
                "revoke:blob:old-stock-brief",
                "remove:src",
                "load",
            ],
        })

    def test_result_audio_can_autoplay_and_notify_when_playback_ends(self):
        program = f"""
          import {{ autoplayResultAudio }} from {json.dumps(RESULT_AUDIO_MODULE.as_uri())};
          const calls = [];
          const listeners = {{}};
          const audio = {{
            autoplay: false,
            playbackRate: 1,
            addEventListener(name, handler) {{ listeners[name] = handler; }},
            play: async function () {{ calls.push(`play:${{this.playbackRate}}`); }},
          }};
          const played = await autoplayResultAudio(audio, {{
            onEnded: () => calls.push("ended"),
          }});
          listeners.ended();
          process.stdout.write(JSON.stringify({{
            played,
            autoplay: audio.autoplay,
            playbackRate: audio.playbackRate,
            calls,
          }}));
        """

        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {
            "played": True,
            "autoplay": True,
            "playbackRate": 1,
            "calls": ["play:1", "ended"],
        })

    def test_hands_free_audio_autoplay_resolves_only_on_ended(self):
        program = f"""
          import {{ playAudioToCompletion }} from {json.dumps(RESULT_AUDIO_MODULE.as_uri())};
          const listeners = new Map();
          const audio = {{
            playbackRate: 0,
            addEventListener(name, handler) {{ listeners.set(name, handler); }},
            removeEventListener(name, handler) {{ if (listeners.get(name) === handler) listeners.delete(name); }},
            play: async () => undefined,
          }};
          let settled = false;
          const pending = playAudioToCompletion(audio).then(value => {{ settled = true; return value; }});
          await Promise.resolve();
          await Promise.resolve();
          const beforeEnded = settled;
          listeners.get("ended")();
          const result = await pending;
          process.stdout.write(JSON.stringify({{beforeEnded, result, playbackRate: audio.playbackRate}}));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program], cwd=PROJECT_ROOT,
            capture_output=True, check=False, text=True, encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {
            "beforeEnded": False,
            "result": "ended",
            "playbackRate": 1,
        })

    def test_hands_free_audio_blocked_autoplay_waits_for_manual_ended(self):
        program = f"""
          import {{ playAudioToCompletion }} from {json.dumps(RESULT_AUDIO_MODULE.as_uri())};
          const listeners = new Map();
          const calls = [];
          const blocked = new Error("gesture required");
          blocked.name = "NotAllowedError";
          const audio = {{
            playbackRate: 0,
            addEventListener(name, handler) {{ listeners.set(name, handler); }},
            removeEventListener(name, handler) {{ if (listeners.get(name) === handler) listeners.delete(name); }},
            play: async () => {{ throw blocked; }},
          }};
          let settled = false;
          const pending = playAudioToCompletion(audio, {{
            onBlocked: name => calls.push(`blocked:${{name}}`),
            onFailure: name => calls.push(`failed:${{name}}`),
          }}).then(value => {{ settled = true; return value; }});
          await new Promise(resolve => setTimeout(resolve, 0));
          const beforeManualPlayback = settled;
          listeners.get("ended")();
          const result = await pending;
          process.stdout.write(JSON.stringify({{beforeManualPlayback, result, calls}}));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program], cwd=PROJECT_ROOT,
            capture_output=True, check=False, text=True, encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {
            "beforeManualPlayback": False,
            "result": "ended",
            "calls": ["blocked:NotAllowedError"],
        })

    def test_hands_free_audio_abort_resolves_cancelled(self):
        program = f"""
          import {{ playAudioToCompletion }} from {json.dumps(RESULT_AUDIO_MODULE.as_uri())};
          const listeners = new Map();
          const controller = new AbortController();
          const audio = {{
            playbackRate: 0,
            addEventListener(name, handler) {{ listeners.set(name, handler); }},
            removeEventListener(name, handler) {{ if (listeners.get(name) === handler) listeners.delete(name); }},
            play: async () => undefined,
          }};
          const pending = playAudioToCompletion(audio, {{signal: controller.signal}});
          controller.abort();
          const result = await pending;
          process.stdout.write(JSON.stringify({{result, listeners: [...listeners.keys()]}}));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program], cwd=PROJECT_ROOT,
            capture_output=True, check=False, text=True, encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {
            "result": "cancelled",
            "listeners": [],
        })

    def test_hands_free_audio_reports_started_only_after_play_resolves(self):
        program = f"""
          import {{ playAudioToCompletion }} from {json.dumps(RESULT_AUDIO_MODULE.as_uri())};
          const listeners = new Map();
          const events = [];
          const audio = {{
            playbackRate: 0,
            addEventListener(name, handler) {{ listeners.set(name, handler); }},
            removeEventListener(name, handler) {{ if (listeners.get(name) === handler) listeners.delete(name); }},
            play: async () => undefined,
          }};
          const pending = playAudioToCompletion(audio, {{onStarted: () => events.push("started")}});
          await new Promise(resolve => setTimeout(resolve, 0));
          const beforeEnded = [...events];
          listeners.get("ended")();
          const result = await pending;
          process.stdout.write(JSON.stringify({{beforeEnded, result}}));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program], cwd=PROJECT_ROOT,
            capture_output=True, check=False, text=True, encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {
            "beforeEnded": ["started"],
            "result": "ended",
        })

    def test_voice_result_uses_server_playback_and_keeps_diagnostics_in_evidence(self):
        source = APP_CLIENT.read_text(encoding="utf-8")
        markup = INDEX_PAGE.read_text(encoding="utf-8")
        self.assertIn("recordSpokenIntent", source)
        self.assertNotIn("SpeechRecognition", source)
        self.assertIn("speakReply", source)
        self.assertIn("stopReplyAudio", source)
        self.assertIn("waitForAudioArtifact", source)
        self.assertNotIn("speechSynthesis", source)
        ask = markup[markup.index('id="ask-view"'):markup.index('id="activity-view"')]
        evidence = markup[markup.index('id="evidence-view"'):]
        self.assertIn('id="assistant-transcript"', ask)
        self.assertNotIn('id="intent-history"', ask)
        self.assertIn('id="intent-history"', evidence)
        self.assertIn('id="intent-history-list"', evidence)

    def test_real_stock_brief_card_keeps_decision_facts_and_removes_provenance_noise(self):
        program = f"""
          import {{ renderInvestingAnswer }} from {json.dumps(INVEST_UI_MODULE.as_uri())};
          const list = {{
            hidden: true,
            innerHTML: "",
            querySelectorAll: () => [],
            querySelector: () => null,
          }};
          const brief = {{
            instrument: {{name: "Cognizant Technology Solutions Corporation", symbol: "CTSH", exchange: "NasdaqGS", quote_currency: "USD"}},
            market: {{price: 60, market_state: "OPEN", trailing_pe: 9, two_week_low: 57, two_week_high: 64, percent_above_two_week_low: 5.26, percent_below_two_week_high: 6.25, one_year_change_pct: -20, captured_at: "2026-10-04T04:00:00+00:00"}},
            requested_allocation: 2000,
            allocation_currency: "USD",
            funding_currency: "SGD",
            allocation_assumption: "I'm treating your $2,000 allocation as US$2,000 because CTSH trades in USD.",
            whole_shares: 33,
            estimated_spend: 1980,
            unallocated_cash: 20,
            fx_estimate: {{pair: "USD/SGD", rate: 1.28, converted_amount: 2560, source: "Yahoo Finance", captured_at: "2026-10-04T04:00:00+00:00", warning: "Indicative only; your broker's executed rate can differ."}},
            funding_account_label: "savings",
            funding_balance: 1000,
            funding_required: 2560,
            funding_shortfall: 1560,
            next_question: "Lower the amount?"
          }};
          renderInvestingAnswer(
            list,
            {{view: "REAL_STOCK_BRIEF", brief, disclaimer: "Timestamped Yahoo Finance research data. No order has been created. Not financial advice."}},
            {{escapeHtml: value => String(value), formatSgd: value => `S$${{Number(value / 100).toFixed(2)}}`}},
            () => {{}},
            () => {{}},
          );
          process.stdout.write(list.innerHTML);
        """

        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Cognizant Technology Solutions Corporation", completed.stdout)
        self.assertIn("CTSH · NasdaqGS", completed.stdout)
        self.assertIn("US$60.00", completed.stdout)
        self.assertIn("Trailing P/E", completed.stdout)
        self.assertIn("US$57.00 – US$64.00", completed.stdout)
        self.assertIn("33 whole shares", completed.stdout)
        self.assertIn("S$2,560.00", completed.stdout)
        self.assertIn("Your savings balance is S$1,000.00", completed.stdout)
        self.assertIn("short S$1,560.00", completed.stdout)
        self.assertIn("Lower the amount?", completed.stdout)
        self.assertNotIn("Snapshot", completed.stdout)
        self.assertNotIn("Yahoo Finance", completed.stdout)
        self.assertNotIn("04/10/2026", completed.stdout)
        self.assertNotIn("broker's executed rate can differ", completed.stdout)
        self.assertNotIn("No order has been created", completed.stdout)
        self.assertNotIn("Not financial advice", completed.stdout)

    def test_voice_failure_preserves_stage_and_maps_permission_denial(self):
        program = f"""
          import {{ describeVoiceFailure }} from {json.dumps(VOICE_FAILURE_MODULE.as_uri())};
          const denied = new Error("permission denied");
          denied.name = "NotAllowedError";
          const worklet = new Error("module load failed");
          worklet.name = "AbortError";
          process.stdout.write(JSON.stringify({{
            denied: describeVoiceFailure("microphone", denied),
            worklet: describeVoiceFailure("worklet_module", worklet),
          }}));
        """

        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {
            "denied": {
                "stage": "microphone",
                "errorName": "NotAllowedError",
                "status": "Microphone access denied · allow it in this website's settings",
            },
            "worklet": {
                "stage": "worklet_module",
                "errorName": "AbortError",
                "status": "iPhone audio processor failed · retry once after reloading",
            },
        })

    def test_voice_capture_resamples_laptop_and_iphone_rates_to_16khz_frames(self):
        program = f"""
          import {{ VoiceFrameResampler }} from {json.dumps(VOICE_CAPTURE_MODULE.as_uri())};
          const laptop = new VoiceFrameResampler(48000);
          const iphone = new VoiceFrameResampler(44100);
          const laptopFrames = laptop.push(new Float32Array(960).fill(0.25));
          const iphoneFrames = iphone.push(new Float32Array(4410).fill(0.25));
          process.stdout.write(JSON.stringify({{
            laptop: laptopFrames.map(frame => frame.length),
            iphone: iphoneFrames.map(frame => frame.length),
          }}));
        """

        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {
            "laptop": [320],
            "iphone": [320, 320, 320, 320, 320],
        })

    def test_voice_audio_context_uses_safari_fallback_without_forcing_sample_rate(self):
        program = f"""
          import {{ createVoiceAudioContext }} from {json.dumps(VOICE_CAPTURE_MODULE.as_uri())};
          let options = null;
          class SafariAudioContext {{
            constructor(received) {{ options = received; this.sampleRate = 44100; }}
          }}
          const context = createVoiceAudioContext({{ webkitAudioContext: SafariAudioContext }});
          process.stdout.write(JSON.stringify({{ options, sampleRate: context.sampleRate }}));
        """

        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {
            "options": {"latencyHint": "interactive"},
            "sampleRate": 44100,
        })

    def test_voice_phase_locks_manual_submit_until_the_automatic_request_finishes(self):
        program = f"""
          import {{ voiceLocksManualSubmit }} from {json.dumps(VOICE_CAPTURE_MODULE.as_uri())};
          process.stdout.write(JSON.stringify({{
            idle: voiceLocksManualSubmit("idle"),
            starting: voiceLocksManualSubmit("starting"),
            listening: voiceLocksManualSubmit("listening"),
            transcribing: voiceLocksManualSubmit("transcribing"),
            complete: voiceLocksManualSubmit("complete"),
            error: voiceLocksManualSubmit("error"),
          }}));
        """

        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {
            "idle": False,
            "starting": True,
            "listening": True,
            "transcribing": True,
            "complete": False,
            "error": False,
        })

    def test_voice_button_presentation_has_distinct_actions_for_every_phase(self):
        program = f"""
          import {{ voiceButtonPresentation }} from {json.dumps(VOICE_CAPTURE_MODULE.as_uri())};
          process.stdout.write(JSON.stringify(Object.fromEntries(
            ["idle", "starting", "listening", "understanding", "speaking", "error"]
              .map(phase => [phase, voiceButtonPresentation(phase)]),
          )));
        """

        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {
            "idle": {"label": "Speak", "ariaLabel": "Start listening", "action": "speak", "disabled": False},
            "starting": {"label": "Cancel", "ariaLabel": "Cancel microphone startup", "action": "stop", "disabled": False},
            "listening": {"label": "Finish", "ariaLabel": "Finish recording and send", "action": "stop", "disabled": False},
            "understanding": {"label": "Processing…", "ariaLabel": "Dimon is processing your request", "action": "processing", "disabled": True},
            "speaking": {"label": "Stop reply", "ariaLabel": "Stop Dimon’s reply", "action": "stop", "disabled": False},
            "error": {"label": "Try again", "ariaLabel": "Try voice input again", "action": "speak", "disabled": False},
        })

        markup = INDEX_PAGE.read_text(encoding="utf-8")
        self.assertIn('class="voice-icon voice-icon-mic"', markup)
        self.assertIn('class="voice-icon voice-icon-stop"', markup)
        self.assertIn('class="voice-icon voice-icon-processing"', markup)

    def test_voice_deadline_fires_once_and_can_be_cancelled(self):
        program = f"""
          import {{ VoiceDeadline }} from {json.dumps(VOICE_CAPTURE_MODULE.as_uri())};
          let scheduled = null;
          let cancelled = null;
          let fired = 0;
          const deadline = new VoiceDeadline(
            (callback, delay) => {{ scheduled = {{ callback, delay }}; return 17; }},
            token => {{ cancelled = token; }},
          );
          deadline.arm(20000, () => {{ fired += 1; }});
          scheduled.callback();
          deadline.clear();
          process.stdout.write(JSON.stringify({{ delay: scheduled.delay, fired, cancelled }}));
        """

        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {"delay": 20000, "fired": 1, "cancelled": None},
        )

    def test_voice_deadline_default_timers_keep_the_global_receiver(self):
        program = f"""
          // Browsers throw "Illegal invocation" when timers get a foreign
          // receiver; make Node's timers equally strict.
          const nativeSet = globalThis.setTimeout;
          const nativeClear = globalThis.clearTimeout;
          globalThis.setTimeout = function (callback, delay) {{
            if (this !== undefined && this !== globalThis) throw new TypeError("Illegal invocation");
            return nativeSet(callback, delay);
          }};
          globalThis.clearTimeout = function (token) {{
            if (this !== undefined && this !== globalThis) throw new TypeError("Illegal invocation");
            return nativeClear(token);
          }};
          const {{ VoiceDeadline }} = await import({json.dumps(VOICE_CAPTURE_MODULE.as_uri())});
          const deadline = new VoiceDeadline();
          let fired = 0;
          deadline.arm(1, () => {{ fired += 1; }});
          await new Promise(resolve => nativeSet(resolve, 20));
          deadline.arm(20000, () => {{ fired += 1; }});
          deadline.clear();
          process.stdout.write(JSON.stringify({{ fired }}));
        """

        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"fired": 1})

    def test_paynow_account_display_exposes_the_demo_proxy(self):
        program = f"""
          import {{ accountDisplay }} from {json.dumps(ACCOUNT_DISPLAY_MODULE.as_uri())};
          const result = accountDisplay({{
            owner_display_name: "JXXX SXXXX",
            paynow_proxy: "8695 2933",
          }});
          process.stdout.write(JSON.stringify(result));
        """

        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "ownerLabel": "Synthetic PayNow",
                "proxyLabel": "PayNow demo · 8695 2933",
            },
        )

    def test_unknown_first_turn_retries_identical_request_coordinates(self):
        program = f"""
          import {{ requestAction }} from {json.dumps(ACTION_MODULE.as_uri())};
          const coordinates = {{
            requestId: "00000000-0000-4000-8000-000000000098",
            text: "send Alicia Powell ten dollars",
          }};
          const calls = [];
          let attempt = 0;
          const fakeFetch = async (url, options) => {{
            calls.push({{url, body: JSON.parse(options.body)}});
            attempt += 1;
            if (attempt === 1) throw new TypeError("response interrupted");
            return {{ok: true, json: async () => ({{transaction_id: "txn_fixed"}})}};
          }};
          const first = await requestAction(coordinates, fakeFetch);
          const second = await requestAction(coordinates, fakeFetch);
          process.stdout.write(JSON.stringify({{first, second, calls}}));
        """

        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["first"]["kind"], "UNKNOWN")
        self.assertEqual(result["second"]["kind"], "RESULT")
        self.assertEqual(
            result["calls"],
            [
                {
                    "url": "/api/actions",
                    "body": {
                        "request_id": "00000000-0000-4000-8000-000000000098",
                        "text": "send Alicia Powell ten dollars",
                    },
                },
                {
                    "url": "/api/actions",
                    "body": {
                        "request_id": "00000000-0000-4000-8000-000000000098",
                        "text": "send Alicia Powell ten dollars",
                    },
                },
            ],
        )

    def test_unknown_outcome_retries_identical_clarification_coordinates(self):
        program = f"""
          import {{ requestClarificationResponse }} from {json.dumps(CLARIFICATION_MODULE.as_uri())};
          const clarification = {{clarification_id: "clarification_fixed"}};
          const requestId = "00000000-0000-4000-8000-000000000099";
          const calls = [];
          let attempt = 0;
          const fakeFetch = async (url, options) => {{
            calls.push({{url, body: JSON.parse(options.body)}});
            attempt += 1;
            if (attempt === 1) throw new TypeError("response interrupted");
            return {{ok: true, json: async () => ({{receipt_choices: []}})}};
          }};
          const first = await requestClarificationResponse(
            clarification, requestId, "ten dollars", fakeFetch
          );
          const second = await requestClarificationResponse(
            clarification, requestId, "ten dollars", fakeFetch
          );
          process.stdout.write(JSON.stringify({{first, second, calls}}));
        """

        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["first"]["kind"], "UNKNOWN")
        self.assertEqual(result["second"]["kind"], "RESULT")
        self.assertEqual(
            result["calls"],
            [
                {
                    "url": "/api/clarifications/clarification_fixed/respond",
                    "body": {
                        "request_id": "00000000-0000-4000-8000-000000000099",
                        "text": "ten dollars",
                    },
                },
                {
                    "url": "/api/clarifications/clarification_fixed/respond",
                    "body": {
                        "request_id": "00000000-0000-4000-8000-000000000099",
                        "text": "ten dollars",
                    },
                },
            ],
        )

    def test_unknown_outcome_can_retry_the_same_receipt_coordinates(self):
        program = f"""
          import {{ requestReceiptApproval }} from {json.dumps(CLIENT_MODULE.as_uri())};
          const choice = {{
            receipt_id: "receipt_fixed",
            receipt_digest: "{'a' * 64}",
          }};
          const calls = [];
          let attempt = 0;
          const fakeFetch = async (url, options) => {{
            calls.push({{url, body: JSON.parse(options.body)}});
            attempt += 1;
            if (attempt === 1) throw new TypeError("response interrupted");
            return {{
              ok: true,
              json: async () => ({{transaction_id: "txn_fixed", idempotent_replay: true}}),
            }};
          }};
          const signed = {{assertion: {{challenge_id: "chal_fixed", signature: "sig"}}}};
          const first = await requestReceiptApproval(choice, signed, fakeFetch);
          const second = await requestReceiptApproval(choice, signed, fakeFetch);
          process.stdout.write(JSON.stringify({{first, second, calls}}));
        """

        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["first"]["kind"], "UNKNOWN")
        self.assertEqual(result["second"]["kind"], "RESULT")
        self.assertEqual(result["second"]["result"]["transaction_id"], "txn_fixed")
        self.assertEqual(
            result["calls"],
            [
                {
                    "url": "/api/receipts/receipt_fixed/approve",
                    "body": {
                        "receipt_digest": "a" * 64,
                        "assertion": {"challenge_id": "chal_fixed", "signature": "sig"},
                    },
                },
                {
                    "url": "/api/receipts/receipt_fixed/approve",
                    "body": {
                        "receipt_digest": "a" * 64,
                        "assertion": {"challenge_id": "chal_fixed", "signature": "sig"},
                    },
                },
            ],
        )

    def test_unknown_batch_approval_retries_identical_coordinates(self):
        program = f"""
          import {{ requestBatchReceiptApproval }} from {json.dumps(BATCH_CLIENT_MODULE.as_uri())};
          const receipt = {{
            receipt_id: "batch_receipt_fixed",
            receipt_digest: "{'a' * 64}",
          }};
          const calls = [];
          let attempt = 0;
          const fakeFetch = async (url, options) => {{
            calls.push({{url, body: JSON.parse(options.body)}});
            attempt += 1;
            if (attempt === 1) throw new TypeError("response interrupted");
            return {{ok: true, json: async () => ({{batch_transaction_id: "batch_txn_fixed"}})}};
          }};
          const signed = {{assertion: {{challenge_id: "chal_fixed", signature: "sig"}}}};
          const first = await requestBatchReceiptApproval(receipt, signed, fakeFetch);
          const second = await requestBatchReceiptApproval(receipt, signed, fakeFetch);
          process.stdout.write(JSON.stringify({{first, second, calls}}));
        """

        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["first"]["kind"], "UNKNOWN")
        self.assertEqual(result["second"]["kind"], "RESULT")
        self.assertEqual(
            result["calls"],
            [
                {
                    "url": "/api/batch-receipts/batch_receipt_fixed/approve",
                    "body": {
                        "receipt_digest": "a" * 64,
                        "assertion": {"challenge_id": "chal_fixed", "signature": "sig"},
                    },
                },
                {
                    "url": "/api/batch-receipts/batch_receipt_fixed/approve",
                    "body": {
                        "receipt_digest": "a" * 64,
                        "assertion": {"challenge_id": "chal_fixed", "signature": "sig"},
                    },
                },
            ],
        )


class ResponsiveTypographyTests(unittest.TestCase):
    def setUp(self):
        self.styles = APP_STYLES.read_text(encoding="utf-8")

    def test_semantic_type_scale_is_defined(self):
        for token in (
            "--type-caption: 0.6875rem",
            "--type-meta: 0.75rem",
            "--type-ui: 0.875rem",
            "--type-body: 1rem",
            "--type-subheading: 1.25rem",
            "--type-heading: 1.625rem",
        ):
            self.assertIn(token, self.styles)

    def test_tiny_and_fractional_pixel_type_is_removed(self):
        self.assertNotRegex(self.styles, r"font-size:\s*9px")
        self.assertNotRegex(self.styles, r"font-size:\s*\d+\.5px")

    def test_mobile_consistency_layer_and_touch_targets_exist(self):
        self.assertIn("@media (max-width: 767px)", self.styles)
        self.assertIn("@media (pointer: coarse)", self.styles)
        self.assertIn(".approval-dialog", self.styles)
        self.assertIn("min-height: 44px", self.styles)


if __name__ == "__main__":
    unittest.main()
