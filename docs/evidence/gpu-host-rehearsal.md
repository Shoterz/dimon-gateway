# GPU host rehearsal — 2026-09-27

This record contains no local paths, process identifiers, prompts, transcripts, audio, tokens, credentials, or personal service configuration.

## Artifact verification

The streaming manifest verifier returned `READY` for all three host artifacts:

| Capability | Bytes | SHA-256 | Revision |
| --- | ---: | --- | --- |
| Qwen intent | 6,522,004,864 | `946072b16f5d672e60357410f900888f6522b3ae49f241b5bf2142cb89637fb6` | `9f646d7eda193ddf2348134f3bff3d49eed7a2c6` |
| Nemotron transcription | 699,872,960 | `d9a01898d2a611c8764e23a1c2f45e70bbd5a425dc4de93692ac951dd603812d` | `ebe59e5a817142986528bbbee5dba8db7b38ed50` |
| Kokoro speech | 327,212,226 | `496dba118d1a58f5f3db2efc88dbdc216e0483fc89fe6e47ee1f2c53f18ad1e4` | `f3ff3571791e39611d31c381e3a41a3af07b4987` |

Both modes reference these same environment-variable destinations. No artifact was copied or downloaded during the rehearsal.

## Live host observations

Baseline GPU telemetry reported 12,282 MiB total, 949 MiB used, and 10,916 MiB free. Qwen was reachable on its expected loopback port. Nemotron was not running. Kokoro was reachable but its pre-existing personal process listened on a wildcard interface instead of loopback. The project gateway was not running.

The safety checks therefore rejected both modes before creating a project process:

- Shared: `NEMOTRON_UNREACHABLE` and `KOKORO_PUBLIC_BIND`.
- Standalone: `QWEN_PORT_OCCUPIED` and `KOKORO_PORT_OCCUPIED`.

This is the intended fail-closed behavior. Starting standalone processes would have duplicated or collided with personal services. Accepting shared mode would have retained an unsafe raw-service network boundary. No project PID record was created.

Running `scripts/stop-host` with no project-owned records left every existing raw listener unchanged. The personal `llama.stocksuite.app` and `jarvis.stocksuite.app` health routes returned HTTP 200 before and after the check.

## Contract rehearsal

The isolated gateway rehearsal exercised one intent request, one streaming transcription, one committed-result speech synthesis, a capacity rejection, and recovery after an upstream process failure. All five checks passed in 0.441 seconds. Fixtures contained no production credential or external network dependency, and content-free observability assertions remain covered by the gateway suite.

## Remaining live acceptance

A full live shared and standalone cycle remains gated by host state rather than repository code. Before rerunning it, the operator must:

1. Reconfigure the existing Kokoro process to bind only to `127.0.0.1:3001`.
2. Start the compatible Nemotron service for shared mode.
3. For the separate standalone rehearsal, stop the owner-managed Qwen and Kokoro listeners through their own service managers so ports 8080 and 3001 are free.
4. Run the shared and standalone checks, exercise the three authenticated live capabilities, and record measured per-capability latency and peak VRAM.

The repository deliberately does not perform those owner-service changes automatically.
