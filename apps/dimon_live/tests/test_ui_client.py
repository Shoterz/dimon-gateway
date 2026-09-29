import json
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
VOICE_CAPTURE_MODULE = (
    PROJECT_ROOT / "apps" / "dimon_live" / "static" / "voice_capture.mjs"
)
VOICE_FAILURE_MODULE = (
    PROJECT_ROOT / "apps" / "dimon_live" / "static" / "voice_failure.mjs"
)


class ReceiptApprovalClientTests(unittest.TestCase):
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
          const assertion = {{challenge_id: "chal_fixed", signature: "sig"}};
          const first = await requestReceiptApproval(choice, assertion, fakeFetch);
          const second = await requestReceiptApproval(choice, assertion, fakeFetch);
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
          const assertion = {{challenge_id: "chal_fixed", signature: "sig"}};
          const first = await requestBatchReceiptApproval(receipt, assertion, fakeFetch);
          const second = await requestBatchReceiptApproval(receipt, assertion, fakeFetch);
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


if __name__ == "__main__":
    unittest.main()
