# User service installation

Create `.env.host` from `.env.example`, fill the host-only paths and Cloudflare Access values, and protect it before rendering units:

```bash
chmod 600 .env.host
scripts/install-user-services --env-file "$PWD/.env.host"
systemctl --user daemon-reload
systemctl --user enable --now dimon-gpu-host.service dimon-inference-gateway.service
```

The installer substitutes absolute repository and environment-file paths. It does not reload systemd, enable a unit, or start a process. The environment file is ignored by Git and must have no group or world permissions.

Inspect status and bounded service logs with:

```bash
systemctl --user status dimon-gpu-host.service dimon-inference-gateway.service
journalctl --user -u dimon-gpu-host.service -u dimon-inference-gateway.service --since today
scripts/healthcheck --json
```

For rollback, stop and disable both units, check out the previous repository revision, render that revision's templates, run `systemctl --user daemon-reload`, and start the units again. Removing the rendered files after disabling them returns systemd to its prior state. None of these commands changes the personal `llama.stocksuite.app` or `jarvis.stocksuite.app` services.
