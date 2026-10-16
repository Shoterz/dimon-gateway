# Nemotron Lifecycle v2 Cutover

This procedure replaces the legacy anonymous active-session counter with capacity-one, ID-bound leases. Run both sides of the protocol together; a v1 client is intentionally incompatible with the v2 supervisor.

## Preconditions

- Confirm the external backups exist in `<gpu-manager>/backups/pre-nemotron-lease-v2-20261014/`.
- Confirm their SHA-256 values:
  - `overlord_nemotron_supervisor.py`: `6e55d0cfa27397a7740c730a4ff5a75f2cd765cad3f717848ec1aa20aa2d73d9`
  - `test_overlord_nemotron.py`: `9e06a9b34f6819f96462afd3409e044cfe8e22ba1e17105569e887a320aed27f`
- Confirm the supervisor and Dimon gateway changes are both installed before restarting either service.
- Run the external supervisor suite and the project lifecycle/gateway suites. Do not cut over with a failing lifecycle test.

## Cutover order

1. Restart `jarvis-nemotron.service`. This controlled restart clears the inherited corrupt anonymous count.
2. Restart `dimon-inference-gateway.service` so every new stream uses protocol v2.
3. Query the Nemotron control socket and GPU Overlord. At idle, `active_sessions` and Nemotron `active_jobs` must both be `0`.
4. Run the bounded acceptance soak:

   ```bash
   .venv/bin/python scripts/verify_nemotron_lifecycle.py \
     --socket /run/user/1000/jarvis-nemotron.ctl.sock \
     --cycles 100
   ```

5. Open a real transcription stream, cancel it, and confirm both aggregate counts return to `0`.
6. Exercise successful finalization, browser disconnect, and an injected upstream failure. Confirm zero after each case.
7. Run one real warm-up. The worker may remain resident and `ready` during its retention window, but it must not remain `busy` and neither aggregate count may grow.

The verifier prints only cycle totals and the final aggregate count. Do not record lease IDs, transcripts, audio, payment data, service keys, or upstream exception details.

## Rollback boundary

Rollback is allowed only before protocol-v2 traffic resumes. Stop both affected services, restore the project revision and the two external backup files as one coordinated change, then restart the supervisor before the gateway. Never restore anonymous counting while a v2 client is active, and never mix a v1 gateway with a v2 supervisor.

## Acceptance record

- Cutover timestamp: `2026-10-14T21:23:03+08:00`
- External supervisor tests: 19 passed, including the 100-cycle registry soak.
- Project lifecycle/gateway tests: 184 passed and 119 subtests passed with the documented timing-boundary test excluded. That one pre-existing hands-free test still fails at 2.001 seconds; it was present before this repair and does not exercise lifecycle ownership.
- Before restart: `active_sessions=90`, `active_jobs=90`.
- After restart idle: `active_sessions=0`, `active_jobs=0`, capacity `1`.
- 100-cycle verifier: passed with final `active_sessions=0`; post-soak `active_jobs=0`.
- Finalization: passed; both aggregate counts returned to `0`.
- Cancellation: passed; both aggregate counts returned to `0`.
- Browser disconnect: passed; both aggregate counts returned to `0`.
- Injected upstream failure: passed; both aggregate counts returned to `0`.
- Warm-up and final idle check: warm-up reported `READY`; Nemotron remained resident in `ready` state with `active_sessions=0` and `active_jobs=0`.
- Classified failures: pre-existing hands-free two-second scheduling boundary only; no lifecycle, capacity, cleanup, protocol, restart, or live-soak failure.
