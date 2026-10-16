from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
MODULE = PROJECT_ROOT / "apps" / "dimon_live" / "static" / "voice_diagnostics.mjs"
APP_CLIENT = PROJECT_ROOT / "apps" / "dimon_live" / "static" / "app.js"
INDEX_PAGE = PROJECT_ROOT / "apps" / "dimon_live" / "static" / "index.html"
APP_STYLES = PROJECT_ROOT / "apps" / "dimon_live" / "static" / "app.css"


class VoiceDiagnosticsUiTests(unittest.TestCase):
    def run_module(self, program: str) -> object:
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", program],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_panel_has_collapsible_hide_and_restore_controls(self) -> None:
        page = INDEX_PAGE.read_text(encoding="utf-8")

        for required in (
            'id="voice-diagnostics"',
            'id="voice-diagnostics-details"',
            'id="voice-diagnostics-hide"',
            'id="voice-diagnostics-show"',
            'id="voice-diagnostics-workers"',
            'id="voice-diagnostics-events"',
            'id="voice-diagnostics-gpu"',
            'id="voice-diagnostics-path"',
        ):
            self.assertIn(required, page)
        self.assertIn("Hide diagnostics", page)

    def test_presentation_covers_every_worker_and_timing_phase(self) -> None:
        program = f"""
          import {{diagnosticsPresentation}} from {json.dumps(MODULE.as_uri())};
          const result = diagnosticsPresentation({{
            trace: {{
              trace_id: "51bda6c7-102e-4ebf-b3d8-981aa60d4aa5",
              events: [
                {{source: "browser", name: "d_click", elapsed_ms: 0}},
                {{source: "browser", name: "audio_ready", elapsed_ms: 8}},
                {{source: "browser", name: "microphone_ready", elapsed_ms: 40}},
                {{source: "browser", name: "socket_ready", elapsed_ms: 55}},
                {{source: "browser", name: "backend_ready", elapsed_ms: 62}},
                {{source: "server", name: "speech_detected", elapsed_ms: 4100, attempt_id: "11111111-1111-4111-8111-111111111111"}},
                {{source: "server", name: "speech_detected", elapsed_ms: 5100, attempt_id: "902512b1-f200-4b61-ab93-c276eafdfd3a"}},
                {{source: "server", name: "nemotron_stream_ready", elapsed_ms: 5130, attempt_id: "902512b1-f200-4b61-ab93-c276eafdfd3a"}},
                {{source: "server", name: "first_partial", elapsed_ms: 5260, attempt_id: "902512b1-f200-4b61-ab93-c276eafdfd3a"}},
                {{source: "server", name: "wake_match", elapsed_ms: 5290, attempt_id: "902512b1-f200-4b61-ab93-c276eafdfd3a"}},
                {{source: "server", name: "speaker_check", elapsed_ms: 5340, attempt_id: "902512b1-f200-4b61-ab93-c276eafdfd3a"}},
                {{source: "browser", name: "browser_wake_received", elapsed_ms: 5410, attempt_id: "902512b1-f200-4b61-ab93-c276eafdfd3a"}},
                {{source: "browser", name: "reply_audio_ready", elapsed_ms: 5580, attempt_id: "902512b1-f200-4b61-ab93-c276eafdfd3a"}},
                {{source: "browser", name: "reply_playback_started", elapsed_ms: 5630, attempt_id: "902512b1-f200-4b61-ab93-c276eafdfd3a"}},
              ],
              attempts: [{{
                attempt_id: "11111111-1111-4111-8111-111111111111",
                path: "failed",
                failure_phase: "wake_not_matched",
              }}, {{
                attempt_id: "902512b1-f200-4b61-ab93-c276eafdfd3a",
                path: "nemotron_final",
                failure_phase: null,
                recognizer_disagreement: true,
                faster_whisper_confidence: "low",
                faster_whisper_avg_logprob: -0.48,
                faster_whisper_no_speech_prob: 0.38,
              }}],
            }},
            workers: [
              {{name: "qwen", loaded: true, device: "cuda"}},
              {{name: "nemotron", loaded: true, device: "cuda"}},
              {{name: "kokoro", loaded: true, device: "cuda"}},
              {{name: "faster_whisper", loaded: true, device: "cuda"}},
            ],
            gpu: {{total_mb: 12282, free_mb: 2213, safety_reserve_mb: 512}},
          }});
          process.stdout.write(JSON.stringify(result));
        """

        result = self.run_module(program)
        self.assertEqual(
            [worker["name"] for worker in result["workers"]],
            ["qwen", "nemotron", "kokoro", "faster_whisper"],
        )
        self.assertTrue(all(worker["loaded"] for worker in result["workers"]))
        self.assertEqual(result["path"], "nemotron_final")
        self.assertIsNone(result["failurePhase"])
        self.assertTrue(result["recognizerDisagreement"])
        self.assertEqual(result["fasterWhisperConfidence"], "low")
        event_names = [event["name"] for event in result["events"]]
        for phase in (
            "d_click",
            "audio_ready",
            "microphone_ready",
            "socket_ready",
            "backend_ready",
            "speech_detected",
            "nemotron_stream_ready",
            "first_partial",
            "wake_match",
            "speaker_check",
            "browser_wake_received",
            "reply_audio_ready",
            "reply_playback_started",
        ):
            self.assertIn(phase, event_names)
        self.assertEqual(event_names.count("speech_detected"), 1)
        self.assertEqual(result["gpu"]["freeMb"], 2213)
        self.assertEqual(result["gpu"]["reserveMb"], 512)

    def test_worker_residency_details_include_pid_profile_heartbeat_and_budget(self) -> None:
        program = f"""
          import {{diagnosticsPresentation}} from {json.dumps(MODULE.as_uri())};
          const result = diagnosticsPresentation({{
            workers: [{{
              name: "qwen", loaded: true, state: "ready", worker_pid: 247840,
              profile: "qwen35-9b-abliterated-q5-4k", active_jobs: 0,
              heartbeat_age_seconds: 0.4, observed_vram_mb: 5852, budget_mb: 6200,
              device: "cuda", compute_type: "Q5_K_M", context_tokens: 4096,
            }}],
          }});
          process.stdout.write(JSON.stringify(result.workers[0]));
        """
        worker = self.run_module(program)
        for evidence in (
            "PID 247840", "qwen35-9b-abliterated-q5-4k", "0 active",
            "0.4 s heartbeat", "5,852 / 6,200 MB",
        ):
            self.assertIn(evidence, worker["detail"])

    def test_partial_fallback_is_rendered_without_fabricated_milestones(self) -> None:
        program = f"""
          import {{diagnosticsPresentation}} from {json.dumps(MODULE.as_uri())};
          const result = diagnosticsPresentation({{
            trace: {{
              trace_id: "51bda6c7-102e-4ebf-b3d8-981aa60d4aa5",
              events: [{{source: "browser", name: "d_click", elapsed_ms: 0}}],
              attempts: [{{
                attempt_id: "902512b1-f200-4b61-ab93-c276eafdfd3a",
                path: "faster_whisper_final",
                failure_phase: "nemotron_ready_timeout",
              }}],
            }},
          }});
          process.stdout.write(JSON.stringify(result));
        """

        result = self.run_module(program)
        self.assertEqual(result["path"], "faster_whisper_final")
        self.assertEqual(result["failurePhase"], "nemotron_ready_timeout")
        self.assertEqual([event["name"] for event in result["events"]], ["d_click"])
        self.assertEqual(len(result["workers"]), 4)
        self.assertFalse(any(worker["loaded"] for worker in result["workers"]))

    def test_visibility_preference_is_persistent_but_does_not_gate_refresh(self) -> None:
        program = f"""
          import {{
            DIAGNOSTICS_VISIBILITY_KEY,
            diagnosticsVisible,
            setDiagnosticsVisible,
          }} from {json.dumps(MODULE.as_uri())};
          const values = new Map();
          const storage = {{
            getItem: key => values.has(key) ? values.get(key) : null,
            setItem: (key, value) => values.set(key, value),
          }};
          const initially = diagnosticsVisible(storage);
          setDiagnosticsVisible(storage, false);
          const hidden = diagnosticsVisible(storage);
          setDiagnosticsVisible(storage, true);
          process.stdout.write(JSON.stringify({{
            key: DIAGNOSTICS_VISIBILITY_KEY,
            initially,
            hidden,
            restored: diagnosticsVisible(storage),
          }}));
        """

        self.assertEqual(self.run_module(program), {
            "key": "dimon.voiceDiagnostics.visible",
            "initially": True,
            "hidden": False,
            "restored": True,
        })
        client = APP_CLIENT.read_text(encoding="utf-8")
        click_handler = client.index('prepareDimonButton?.addEventListener("click"')
        warmup = client.index("beginResetWarmupFromGesture", click_handler)
        trace = client.index("beginTrace", click_handler)
        self.assertLess(trace, warmup)
        handler = client[click_handler:warmup + 500]
        self.assertIn("unlockAudioFromGesture", handler)
        self.assertIn("resetAndWarmPreparation", handler)
        self.assertIn("prepareFromGesture", handler)

    def test_renderer_uses_text_nodes_and_mobile_panel_collapses(self) -> None:
        module = MODULE.read_text(encoding="utf-8")
        styles = APP_STYLES.read_text(encoding="utf-8")

        self.assertIn("textContent", module)
        self.assertNotIn("innerHTML", module)
        self.assertIn('matchMedia("(max-width: 640px)")', module)
        self.assertRegex(
            styles,
            r"@media\s*\(max-width:\s*640px\)[\s\S]*?\.voice-diagnostics",
        )


if __name__ == "__main__":
    unittest.main()
