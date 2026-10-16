# User service installation

Create `.env.host` from `.env.example`, fill the host-only paths, generate a strong `DIMON_INFERENCE_SERVICE_KEY`, and protect it before rendering units. Give authorized teammates the same key through a secure channel; never commit it:

```bash
chmod 600 .env.host
scripts/install-user-services --env-file "$PWD/.env.host"
systemctl --user daemon-reload
systemctl --user enable --now dimon-gpu-host.service dimon-faster-whisper-medium.service dimon-inference-gateway.service
```

The installer substitutes absolute repository and environment-file paths in three project units and two integration drop-ins. It does not reload systemd, enable a unit, or start a process. The environment file is ignored by Git and must have no group or world permissions.

Install Faster-Whisper with `scripts/install-faster-whisper-medium --install`.
Its Python environment is `.runtime/faster-whisper-medium/venv`, its pinned
model is `.runtime/models/faster-whisper-medium`, and its package/model caches
remain under `.runtime/cache`. In `.env.host`, point
`DIMON_FASTER_WHISPER_PYTHON` and `DIMON_FASTER_WHISPER_MODEL_PATH` at those
absolute paths, then set `DIMON_WHISPER_URL=ws://127.0.0.1:8007/ws` and
`DIMON_WHISPER_REVISION=faster-whisper-medium-cuda`.

Inspect status and bounded service logs with:

```bash
systemctl --user status dimon-gpu-host.service dimon-faster-whisper-medium.service dimon-inference-gateway.service
journalctl --user -u dimon-gpu-host.service -u dimon-faster-whisper-medium.service -u dimon-inference-gateway.service --since today
scripts/healthcheck --json
```

For rollback, point `DIMON_WHISPER_URL` back to the explicitly retained CPU
service on `ws://127.0.0.1:8003/ws`, stop and disable the Medium unit, check out
the previous repository revision, render that revision's templates, run
`systemctl --user daemon-reload`, and start the prior units again. Removing the
rendered files after disabling them returns systemd to its prior state. None of
these commands changes the personal `llama.stocksuite.app` or
`jarvis.stocksuite.app` services.
