# GPU host setup

The GPU host has two modes. `shared` reuses compatible Qwen, Nemotron, and Kokoro processes already listening on the three loopback ports and starts only Dimon's inference gateway. When Nemotron is managed on demand, configure `DIMON_NEMOTRON_CONTROL_SOCKET` so transcription acquires and releases the existing supervisor. `standalone` starts repository-owned processes while referencing the same external artifact files. Neither mode copies weights.

## First run

Install the locked lightweight project environment and create ignored host configuration:

```bash
uv sync --frozen
cp .env.example .env.host
cp config/models.example.yaml config/models.yaml
```

Set the artifact and runtime path variables in `.env.host`. Set `DIMON_QWEN_REVISION`, `DIMON_NEMOTRON_REVISION`, and `DIMON_KOKORO_REVISION` to the immutable revisions in `config/models.example.yaml`. Set `DIMON_INFERENCE_SERVICE_KEY` to a rotated value distributed out of band to authorized teammates. Restrict the host file with `chmod 600 .env.host`.

The Cloudflare Tunnel hostname `tencentservices.stocksuite.app` should target
`http://127.0.0.1:8891`. Only the gateway is published; model services remain
on their private loopback ports.

Normal setup does not download anything:

```bash
scripts/setup-host --mode shared --check
scripts/setup-host --mode standalone --check
```

The standalone check is expected to report occupied ports while shared services are running. Stop those services through their own project before starting standalone mode. An operator who has accepted every recorded license can explicitly download missing weights with `--download --accept-licenses`; the command prints license, size, checksum, and destination variable and requires typing `DOWNLOAD`. Add `--yes` only in an attended automation environment.

## Start and stop

Run shared mode when the personal loopback processes are already available:

```bash
scripts/start-host --mode shared
```

Run standalone mode only after the raw ports are free:

```bash
scripts/start-host --mode standalone
```

Both commands remain in the foreground and record only project-owned child identities under `${XDG_RUNTIME_DIR}/dimon-gateway/`. Stop with `Ctrl-C` or, from another terminal, `scripts/stop-host`. PID identity and command hashes are checked before any process group receives a signal; stale or foreign records are deleted without signalling the process.

## Health, logs, and recovery

```bash
scripts/healthcheck --json
ls "${XDG_RUNTIME_DIR:-/tmp}/dimon-gateway/"
```

Health reports fixed capability names, safe reason codes, revision agreement, and aggregate free VRAM. It omits paths, prompts, transcripts, audio, tokens, and credentials. Logs are capped by rotation at 2 MiB per managed service.

For an upgrade, stop Dimon, update the repository, run `uv sync --frozen`, re-run both the manifest and mode check, then start it again. For rollback, check out the previous repository revision and restore its matching ignored environment values; external model files remain in place. The application-level gateway queues remain the final request limits even when the host has spare VRAM.
