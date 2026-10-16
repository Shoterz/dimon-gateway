# Faster-Whisper Medium CUDA Operations

This service is Dimon's normal authoritative speech final. Nemotron continues
to provide streaming partials; Faster-Whisper Medium provides the complete text
used by the intent pipeline.

## Fixed identity and ownership

- Model: `medium`
- Device: `cuda`
- Compute type: `int8_float16`
- Public loopback frontend: `ws://127.0.0.1:8007/ws`
- Private model worker: `127.0.0.1:8008`
- GPU Overlord service/profile: `faster_whisper/faster-whisper-medium-cuda`
- Runtime: `.runtime/faster-whisper-medium/venv`
- Model: `.runtime/models/faster-whisper-medium`
- Caches: `.runtime/cache`

The systemd frontend PID is not residency evidence. `GET /health` is ready only
when it reports a separate `worker_pid`, the exact identity above, and a recent
successful model probe. That PID must also appear in GPU Overlord and
`nvidia-smi`.

## Install and offline verification

From the repository root:

```bash
scripts/install-faster-whisper-medium --install
scripts/install-faster-whisper-medium --check
```

The installer uses the hash-locked package graph and immutable model revision
in `services/faster_whisper_cuda/`. It sets `PYTHONNOUSERSITE=1` and redirects
uv, pip, Hugging Face, Transformers, and XDG caches beneath `.runtime`.
`--check` is offline and fails if imports resolve through `.local`, the project
`.venv`, or another Python environment.

The nested NVIDIA cuBLAS and cuDNN library directories are placed first in
`LD_LIBRARY_PATH` before the child Python process starts. A missing CUDA device,
wrong model marker, or wrong effective compute identity fails closed; there is
no automatic CPU fallback.

## Private host configuration

Set these absolute paths in the ignored mode-600 `.env.host`:

```dotenv
DIMON_FASTER_WHISPER_PYTHON=/absolute/repository/.runtime/faster-whisper-medium/venv/bin/python
DIMON_FASTER_WHISPER_MODEL_PATH=/absolute/repository/.runtime/models/faster-whisper-medium
DIMON_GPU_OVERLORD_CLIENT_DIR=/absolute/path/to/gpu-process-manager
DIMON_GPU_OVERLORD_SOCKET=/run/user/1000/gpu-overlord-1000.sock
DIMON_WHISPER_URL=ws://127.0.0.1:8007/ws
DIMON_WHISPER_REVISION=faster-whisper-medium-cuda
```

Render and start the service:

```bash
scripts/install-user-services --env-file "$PWD/.env.host"
systemctl --user daemon-reload
systemctl --user enable --now dimon-faster-whisper-medium.service
```

## Health, probe, and logs

```bash
curl -fsS http://127.0.0.1:8007/health
curl -fsS -X POST http://127.0.0.1:8007/warm
curl -fsS -X POST http://127.0.0.1:8007/probe
journalctl --user -u dimon-faster-whisper-medium.service --since today
```

Healthy residency requires `ready=true`, `loaded=true`, model `medium`, device
`cuda`, compute type `int8_float16`, a positive `worker_pid`, zero idle jobs,
and a non-null `last_probe_at`. Health and logs contain operational state only;
they must never include audio or transcript text.

To capture the measured four-worker budget, warm all four real model workers,
start representative requests, and run:

```bash
.venv/bin/python scripts/calibrate_resident_vram.py \
  --duration 30 \
  --output docs/benchmarks/2026-10-14-resident-worker-vram.json
```

The report binds samples to verified worker PIDs and rejects less than 512 MiB
free VRAM.

## Truthful four-worker readiness

The red-D backend status is strict. `READY` requires all five stages
(`capacity`, `nemotron`, `qwen`, `kokoro`, and `faster_whisper`) plus four
distinct resident worker PIDs. Check the sanitized status through Dimon:

```bash
curl -fsS -X POST -H 'Content-Type: application/json' -d '{}' \
  http://127.0.0.1:8890/api/demo/prepare
curl -fsS http://127.0.0.1:8890/api/demo/prepare
```

On 2026-10-15, a hot revalidation completed in 3.452 seconds and the same four
workers remained resident 82 seconds later with 2,261 MiB free. Nemotron had
zero active sessions after the check. The content-free evidence is in
`docs/benchmarks/2026-10-15-four-worker-readiness.json`.

A completely cold concurrent launch can briefly return `DEGRADED` because the
GPU arbiter conservatively double-counts startup reservations while models are
still loading. The all-worker monitor retries and only changes the badge to
`READY` after final residency succeeds. Do not weaken the gate or interpret the
individual successful probes as proof of simultaneous residency.

## Measure the red-D to “Hey Dimon” path

The in-app Voice diagnostics panel is content-free. It shows actual residency
for Qwen, Nemotron, Kokoro, and Faster-Whisper Medium; the GPU free/reserve
margin; the selected recognition path; a bounded Nemotron failure phase; and
the latest timing waterfall. Worker cards include the resident PID, exact
profile, heartbeat age, active jobs, and observed/budgeted VRAM. `Hide
diagnostics` only hides the presentation.
The measurement remains active, and the small Diagnostics control restores the
panel after recording.

Start a 20-trial capture from the repository root:

```bash
.venv/bin/python scripts/capture_voice_trace.py \
  --trials 20 \
  --output docs/benchmarks/2026-10-15-hey-dimon-latency.json
```

For each prompt printed by the command:

1. Click the red D once.
2. Wait five seconds.
3. Say “Hey Dimon” once and allow the spoken reply to start.
4. Wait for the command to confirm that trace before beginning the next trial.

The command reads only `GET /api/voice/diagnostics/latest`. Its report contains
UUIDs, timestamps, allowlisted phase durations, recognition/failure labels,
worker PID/profile/heartbeat residency, and GPU numbers. Each record selects
one terminal attempt, so background speech cannot be mixed with the measured
“Hey Dimon” attempt. It refuses transcript, audio, prompt, payee,
URL, command, socket, and filesystem fields. Browser timings are measured from
the D click; backend timings use the backend trace origin. They are summarized
in separate lanes and are never subtracted across clocks.

Review `summary.recognition_paths` first. A healthy Nemotron fast path records
`nemotron_partial`; a CUDA authoritative fallback records
`faster_whisper_final` with a bounded `failure_phase` when Nemotron failed.
Then compare the browser and server p50/p95 milestones and confirm all four
workers remained loaded with GPU free memory above the safety reserve.
`reply_audio_ready` means the audio blob was attached to the browser player;
`reply_playback_started` is recorded only after the browser accepted
`audio.play()`.

## Rollback

1. Restore the backed-up Qwen supervisor mapping and GPU Overlord configuration.
2. Set `DIMON_WHISPER_URL=ws://127.0.0.1:8003/ws` and the prior CPU revision in
   the private environment files.
3. Restart the inference gateway and Dimon backend.
4. Stop and disable `dimon-faster-whisper-medium.service`.
5. Confirm the service no longer has a GPU Overlord lease or worker PID.

The original external files are under:

```text
<gpu-manager>/backups/pre-faster-whisper-medium-qwen4k-20261014/
```

## Complete project-local cleanup

After stopping and disabling the unit, remove only these repository-owned
trees if cleanup is desired:

```text
.runtime/faster-whisper-medium
.runtime/models/faster-whisper-medium
.runtime/cache
```

No `~/.local` or system Python cleanup is required because installation never
writes there.
