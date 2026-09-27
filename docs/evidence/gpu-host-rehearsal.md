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

## Safety gate and host correction

Baseline GPU telemetry reported 12,282 MiB total, 949 MiB used, and 10,916 MiB free. The first preflight rejected shared mode because Nemotron was idle and the existing Kokoro frontend used a wildcard bind. It rejected standalone mode because owner-managed Qwen and Kokoro listeners occupied the fixed ports. No project process was created by either rejected check.

During an approved maintenance window, the existing Kokoro frontend default was changed to loopback and its owner-managed stack was restarted. Nemotron was acquired through its existing private controller. Qwen, Nemotron, and Kokoro then listened on their expected loopback ports and `scripts/setup-host --mode shared --check` returned `ready`.

## Shared-mode rehearsal

Shared mode created only the project gateway record and reused the three existing raw listeners. All four health entries returned `READY`, with revision agreement for every model. Live capability results were:

| Capability | Result | Measured latency |
| --- | --- | ---: |
| Qwen intent | `BENEFICIARY_TRANSFER` | 2,550.24 ms |
| Kokoro speech | 97,244-byte PCM16 WAV | 177.70 ms |
| Nemotron transcription | Non-empty 28-character result | 161.13 ms |

After `scripts/stop-host`, the three shared raw listener records were byte-for-byte unchanged and the project gateway was gone. This proves shared cleanup did not signal a personal process.

## Standalone-mode rehearsal

The owner-managed GPU stack and Nemotron controller were stopped for the approved test window. Standalone preflight returned `ready`, then Dimon created four project PID records and started Qwen, Nemotron, Kokoro, and the gateway on loopback. The launcher arguments selected the same configured Qwen and Kokoro artifact identities; Nemotron used the same verified model store and immutable model alias. No artifact was downloaded or copied.

All health entries returned `READY`. Live capability results were:

| Capability | Result | Measured latency |
| --- | --- | ---: |
| Qwen intent | `BENEFICIARY_TRANSFER` | 2,575.79 ms |
| Kokoro speech | 97,244-byte PCM16 WAV | 823.58 ms |
| Nemotron transcription | Non-empty 28-character result | 152.61 ms |

Peak observed GPU usage was 8,897 MiB, leaving 2,968 MiB free. A live attempt to reserve another 7,168 MiB Qwen profile was rejected as `GPU_CAPACITY_UNAVAILABLE` without spawning a process.

The first cycle exposed two implementation faults: process identity changed across launcher `exec`, and the external Kokoro environment lacked an optional WAV package. Identity now uses boot ID plus process start ticks, and the repository-owned Kokoro service writes PCM16 WAV with the standard library. A controlled project-only restart then passed all capabilities, providing restart-recovery evidence.

## Restoration

`scripts/stop-host` removed every project PID record and released the raw ports. The personal GPU stack was restored under its original transient unit name and the Nemotron controller was restarted. Both owner-managed services reported active. The personal `llama.stocksuite.app` and `jarvis.stocksuite.app` health routes returned HTTP 200 after restoration, and Kokoro remained loopback-only.

The isolated contract suite also covers authenticated gateway envelopes, content-free observability, bounded queues, and stable upstream failures without production credentials.
