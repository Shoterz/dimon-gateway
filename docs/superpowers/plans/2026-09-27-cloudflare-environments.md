# Cloudflare Environments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish stable Dimon demo, inference, and staging hostnames through Cloudflare Tunnel and Access while preserving the owner's personal llama and Jarvis routes.

**Architecture:** Create a dedicated named tunnel for the hackathon. Route `dimon.stocksuite.app` to the public Dimon app on loopback port 8890, `dimon-inference.stocksuite.app` to the Access-protected inference gateway on 8891, and `dimon-staging.stocksuite.app` to the Access-protected integration app on 8892. Raw model ports remain absent from tunnel ingress.

**Tech Stack:** Cloudflare Tunnel, Cloudflare Access, cloudflared, FastAPI, WebSockets, systemd user services, curl.

**Spec:** [Team repository and inference architecture design](../specs/2026-09-27-team-repository-and-inference-architecture-design.md)

**Platform references:** [Cloudflare Tunnel configuration](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel-api/), [Access service tokens](https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/), and [Access JWT validation](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/).

## Global Constraints

- This plan depends on the gateway and GPU-host checkpoints.
- Preserve `llama.stocksuite.app` and `jarvis.stocksuite.app` records, routes, policies, and origins exactly.
- No ingress rule may target Qwen 8080, Nemotron 8004, Kokoro 3001, SQLite, or a filesystem path.
- `dimon.stocksuite.app` is public; inference and staging require Cloudflare Access.
- Tunnel credentials, Access client secrets, account IDs, zone IDs, and rendered host config remain outside Git.
- Browser CORS applies only to the public/staging application APIs. Model calls originate from the Dimon backend.

## Review Focus

- The final ingress rule must be a catch-all 404.
- Access must protect HTTP and WebSocket upgrades consistently.
- Team service tokens must be scoped to the inference application and individually revocable.
- Origin services must stay bound to loopback if the tunnel stops.
- Existing personal DNS and tunnel routes must survive apply and rollback checks unchanged.

---

### Task 1: Capture current state and define repository-owned templates

**Files:**
- Create: `deploy/cloudflare/tunnel-config.example.yml`
- Create: `deploy/cloudflare/access-policy.example.json`
- Create: `deploy/cloudflare/README.md`
- Create: `scripts/check-cloudflare-config`
- Create: `tests/test_cloudflare_config.py`

**Interfaces:**
- The tunnel template has ordered ingress for the three Dimon hostnames and final `service: http_status:404`.
- `scripts/check-cloudflare-config` verifies hostnames, ports, catch-all ordering, loopback origins, Access expectations, and absence of secrets/personal routes.
- The template uses placeholders for tunnel UUID and credential path; rendered configuration is ignored.

- [ ] Write failing tests for missing hostnames, wrong ports, raw-model ports, public bind addresses, missing catch-all, secrets, and accidental personal-route edits.
- [ ] Run `uv run python -m unittest tests.test_cloudflare_config -v`; confirm the templates/checker are absent.
- [ ] Export and save a sanitized inventory of the current DNS, tunnels, ingress, and Access applications outside Git for rollback comparison.
- [ ] Add repository templates and the checker with the approved route table.
- [ ] Document the dedicated tunnel name `dimon-hackathon`, origin ports, policy intent, and rollback sequence.
- [ ] Run the focused tests and repository-boundary checker.
- [ ] Commit the templates as `ops: define Dimon Cloudflare routes`.

### Task 2: Tighten application origin and CORS contracts

**Files:**
- Modify: `apps/dimon_live/app.py`
- Modify: `apps/dimon_live/session_manager.py`
- Modify: `.env.example`
- Create: `apps/dimon_live/tests/test_cloudflare_origin.py`

**Interfaces:**
- Production allowed origins are exactly `https://dimon.stocksuite.app` and `https://dimon-staging.stocksuite.app`.
- Host validation accepts only the configured environment hostname and local test host.
- Owner/staging identity uses verified Cloudflare Access claims; public visitors receive opaque isolated session cookies.
- WebSocket origin and host checks run before voice resources are acquired.

- [ ] Write failing tests for allowed origins, evil suffixes, missing origins on browser mutation, preflight behavior, wrong host, forged Access headers, and WebSocket origin rejection before lease acquisition.
- [ ] Run the focused test and observe failures.
- [ ] Centralize exact origin/host matching and environment-specific configuration.
- [ ] Keep public `GET` assets usable while requiring origin checks on browser state-changing routes.
- [ ] Run the origin, session, public-boundary, and voice tests.
- [ ] Commit origin hardening as `fix: enforce deployment origins`.

### Task 3: Create the dedicated tunnel and DNS routes

**Files:**
- Create locally but ignore: `deploy/cloudflare/tunnel-config.yml`
- Create locally but ignore: `deploy/cloudflare/current-state.json`
- Modify: `deploy/cloudflare/README.md`

**Interfaces:**
- Named tunnel: `dimon-hackathon`.
- DNS routes: the three approved hostnames only.
- Origins: `http://127.0.0.1:8890`, `http://127.0.0.1:8891`, and `http://127.0.0.1:8892`.

- [ ] Confirm all three origin ports are healthy locally and raw model ports listen only on loopback.
- [ ] Create the named tunnel, write its credential file with owner-only permissions, and render the ignored local configuration from the checked-in template.
- [ ] Create or update only the three approved CNAME routes after comparing current DNS state.
- [ ] Run `cloudflared tunnel ingress validate` and `cloudflared tunnel ingress rule` for each hostname plus an unknown hostname.
- [ ] Compare the personal llama/Jarvis records and tunnel routes to the saved inventory; stop and roll back if either changed.
- [ ] Commit only the sanitized documentation update as `ops: document Dimon tunnel setup`; keep rendered credentials and state untracked.

### Task 4: Configure public, human, and service-token Access policies

**Files:**
- Modify: `deploy/cloudflare/access-policy.example.json`
- Modify: `deploy/cloudflare/README.md`
- Create: `docs/cloudflare-access.md`

**Interfaces:**
- `dimon.stocksuite.app`: no Access application; public application protections remain in FastAPI.
- `dimon-staging.stocksuite.app`: Access application allowing the named team emails or approved identity group.
- `dimon-inference.stocksuite.app`: Access application with a team-human policy and per-teammate/per-CI service-token policies.
- Gateway validates the configured inference Access audience.

- [ ] Create the two Access applications and record their non-secret audience tags in the ignored host environment.
- [ ] Create individually named service tokens with the shortest practical hackathon lifetime; deliver each secret out of band and never commit it.
- [ ] Verify no-token, invalid-token, expired-token, valid-human, and valid-service-token behavior for HTTP.
- [ ] Verify the same identity outcomes during a WebSocket upgrade to `/v1/transcription`.
- [ ] Document revocation, rotation, teammate offboarding, and post-hackathon shutdown commands.
- [ ] Commit the policy documentation as `docs: document inference access policy`.

### Task 5: Install and supervise the tunnel without touching personal services

**Files:**
- Create: `deploy/systemd/dimon-app.service`
- Create: `deploy/systemd/dimon-staging.service`
- Create: `deploy/systemd/dimon-cloudflared.service`
- Modify: `deploy/systemd/README.md`
- Create: `tests/test_cloudflared_unit.py`

**Interfaces:**
- The public app unit binds `127.0.0.1:8890`; the staging app unit binds `127.0.0.1:8892`. Both use ignored environment files and the repository's `dimon-serve` entry point.
- A dedicated user service runs `cloudflared tunnel --config <ignored-rendered-config> run dimon-hackathon`.
- The tunnel starts after both local Dimon app units and the inference gateway, restarts with bounded delay, and reads credentials from the ignored Cloudflare directory.

- [ ] Write failing unit-contract tests for both app ports, loopback binds, separate environment files, the dedicated tunnel name, config argument, user-level execution, startup ordering, restart policy, and absence of embedded credentials or personal unit names.
- [ ] Run the focused test and observe failures.
- [ ] Add the three unit templates and installer instructions; configure public and staging as separate app processes and do not replace or edit the personal `cloudflared.service`.
- [ ] Verify the rendered unit with `systemd-analyze --user verify`, then enable the dedicated unit.
- [ ] Restart the dedicated service and prove the personal tunnel process and routes were unaffected.
- [ ] Commit the unit template as `ops: add dedicated Dimon tunnel service`.

### Task 6: Run external routing, security, and rollback rehearsals

**Files:**
- Create: `scripts/verify-deployment`
- Create: `docs/evidence/cloudflare-rehearsal.md`
- Modify: `docs/cloudflare-access.md`

**Interfaces:**
- `scripts/verify-deployment` checks public app status, inference denial/acceptance, staging denial/acceptance, WebSocket upgrade, security headers, CORS, and hostname isolation without printing tokens.

- [ ] Write script-contract tests using mocked HTTP results before calling live hostnames.
- [ ] Verify `dimon.stocksuite.app` loads as a new visitor and cannot access private health/model routes.
- [ ] Verify inference and staging deny anonymous users, accept the intended identities, preserve request correlation, and support the transcription WebSocket.
- [ ] Probe raw service paths and unexpected hostnames; confirm the tunnel returns denial or 404 and never proxies raw model APIs.
- [ ] Disable the dedicated tunnel, execute the documented rollback, and confirm the personal hostnames still work; then restore the tunnel.
- [ ] Record sanitized timestamps, status codes, policy outcomes, and latency without tokens, personal email addresses, or request content.
- [ ] Commit the deployment evidence as `test: record Cloudflare deployment rehearsal`.

### Verification Contract

Before reporting this checkpoint complete, run:

```bash
uv run python -m unittest tests.test_cloudflare_config -v
uv run python -m unittest tests.test_cloudflared_unit -v
uv run python -m unittest apps.dimon_live.tests.test_cloudflare_origin -v
scripts/check-cloudflare-config deploy/cloudflare/tunnel-config.example.yml
cloudflared tunnel ingress validate
scripts/verify-deployment
scripts/check-repository-boundary
git diff --check
```

Complete the checkpoint only when all three stable hostnames behave as specified, raw ports remain unreachable through the tunnel, HTTP and WebSocket Access behavior agree, rollback has been exercised, and the saved before/after comparison shows no change to `llama.stocksuite.app` or `jarvis.stocksuite.app`.
