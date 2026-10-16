# Shared Qwen 8K and Concurrent Kokoro Residency

## Goal

Reduce the shared Qwen3.5 serving context to 8K and have DIMON warm Nemotron, Qwen3.5, and Kokoro together so that speech synthesis can begin as soon as a response is available, without waiting for Kokoro to cold-start.

## Approved scope and constraints

- Qwen3.5's 8K context applies to the shared Qwen service, including `llama.stocksuite.app`; the user explicitly accepts this cross-site effect.
- Nemotron, Qwen3.5, and Kokoro should be resident together when DIMON reports inference preparation ready.
- Qwen and Kokoro warm-up should overlap, rather than warming Kokoro only after Qwen has completed its warm-up.
- Preserve the GPU Overlord as the authority for VRAM leases and eviction. Do not bypass it or claim readiness from an HTTP health check alone.
- Do not change stock or banking decision behavior, credentials, routes, schemas, persisted state, or ledger behavior.
- Do not interrupt, evict, or report ready while another managed worker is busy or the three-model capacity check fails.

## Current architecture and evidence

- DIMON's local provider factory and the other site use the same loopback Qwen API endpoint (`127.0.0.1:8080/v1`); the Qwen supervisor currently selects 16K when Nemotron is active and 32K otherwise.
- The GPU Overlord already grants independent leases to `nemotron`, `llama` (Qwen), and `kokoro` services and can cooperatively evict idle workers when admission fails.
- The current GPU profiles include a 7,168 MiB Qwen profile while Nemotron is active, 1,536 MiB for Nemotron, 1,280 MiB for Kokoro, and a 512 MiB reserve. These totals suggest coexistence may fit, but are budget estimates, not proof of measured peak use.
- DIMON has a best-effort demo-preparation coordinator that currently warms capacity, Nemotron, Qwen, and Kokoro sequentially. It validates Qwen extraction and a RIFF/WAVE Kokoro result.
- A previous live snapshot showed an RTX 4070 with 12,282 MiB total, Qwen and Nemotron in use, and about 4,746 MiB free. Kokoro's current process/lease state was not warm in that snapshot.

## Design

### 1. Shared Qwen 8K profile

For `qwen35-9b-abliterated-q5`, the managed Qwen supervisor will always launch with `CTX_SIZE=8192`, independent of Nemotron's active state. The shared Qwen model and API endpoint remain the same; only the Qwen context profile changes. Other model profiles and launch behavior remain unchanged.

Add an explicit 8K GPU Overlord profile for Qwen. Its VRAM budget must be set from an observed cold-start and representative generation peak on this host, including allocator overhead. Do not estimate the budget by scaling the 16K profile linearly. Keep the existing GPU safety reserve unchanged unless measurements show it cannot be met; any reserve change requires a separate decision.

### 2. Trio pre-warm and overlap

When local inference and `DIMON_DEMO_PREPARE_ENABLED=1` are configured, DIMON starts the existing best-effort preparation in a background task during application startup; HTTP/page startup must not wait for model loading. The existing local-operator status endpoint remains available, and its POST operation remains a way to start/retry preparation.

Preparation first performs the existing capacity preflight and ensures Nemotron is ready. It then starts Qwen's extraction probe and Kokoro's synthesis probe concurrently. Each probe must use the existing managed service/client path so that both services hold valid leases during warm-up.

Readiness is reported only after both probes succeed and a subsequent Overlord status snapshot confirms live, non-error workers for Nemotron, Qwen, and Kokoro. The Kokoro probe audio is discarded after RIFF/WAVE validation. Preparation timeouts remain bounded, cancellation is propagated safely, and a failed/evicted worker changes readiness to degraded rather than being presented as warm.

Leases remain managed by their current supervisors and normal idle/eviction rules. No permanent pin, unbounded lease, or protection from legitimate GPU pressure is introduced. During actual generation, the broker must continue to protect busy services from eviction. If capacity is insufficient, the application reports preparation degraded and retains its existing text-response/TTS-failure behavior rather than overcommitting VRAM.

### 3. User-visible behavior

DIMON begins preparation automatically without blocking page/API startup. Its existing preparation status exposes whether each of the three services is ready or which stage failed. Once ready, a normal Qwen answer can be handed immediately to the already-loaded Kokoro service; no extra user action is introduced. If preparation has not completed when an answer arrives, existing synthesis behavior continues, while status remains PREPARING/DEGRADED rather than implying that Kokoro was already warm. This work does not itself redesign streaming audio or the conversation UI.

## Alternatives considered

1. **Shared Qwen at 8K plus managed concurrent warm-up (recommended).** Fits the user's accepted cross-site context change and reuses the existing Overlord and endpoints. Requires careful measured GPU-budget calibration.
2. **Keep Qwen at 16K/32K and load a dedicated DIMON Qwen instance.** Better site isolation, but a second 9B model is not supported by the observed available VRAM and adds a second endpoint/supervisor to maintain.
3. **Keep the current shared context and warm Kokoro only after Qwen.** Lowest change risk, but it does not meet the request to have Kokoro loaded while Qwen is working.

## Failure and resource policy

- Readiness requires all three named services to be observed resident, not merely that warm-up requests returned successfully.
- Warm-up timeout, lease denial, process crash, missing worker, or malformed Kokoro output yields a stable failed/degraded stage; do not expose raw upstream exceptions or environment details.
- Never terminate a busy service to satisfy warm-up. GPU Overlord's lease/admission policy remains authoritative.
- Measured peak use above the configured budget blocks declaring the trio ready until a suitable profile is calibrated.
- Normal Qwen and Kokoro request handling remains available according to existing behavior when optional background preparation fails; the system must not deadlock waiting indefinitely for all warmers.

## Acceptance tests

- Qwen3.5 supervisor resolution selects `CTX_SIZE=8192` whether Nemotron is active, inactive, or status cannot be read; other model configurations remain unchanged.
- The Qwen profile is distinct and admission accounting for Qwen 8K + Nemotron + Kokoro + the safety reserve is exercised with both sufficient- and insufficient-capacity snapshots.
- DIMON tests prove Qwen and Kokoro warm probes overlap (both start before either completes), and preparation reaches ready only after both complete successfully and the final status confirms all three workers.
- Tests cover each warmer's timeout/failure, cancellation/cleanup, malformed Kokoro output, worker eviction before readiness, and busy-worker protection.
- A service-level check confirms the shared Qwen endpoint reports/launches an 8,192-token context and that the llama.stocksuite.app route still reaches the same endpoint, with no unrelated model or site configuration changed.
- On-host validation records GPU free/used memory before and during all three services, confirms all three remain resident through a Qwen generation followed by Kokoro synthesis, and checks Qwen, Nemotron, and Kokoro outputs remain functional.

## Rollout and rollback

Apply the new Qwen profile through the managed GPU stack, restart the stack in a controlled window, then run the DIMON warm-up and live memory checks. Do not delete or alter unrelated service files. If the measured 8K Qwen plus Nemotron plus Kokoro use breaches the reserve or causes worker instability, restore the prior Qwen profile selection and profile mapping, then report the capacity evidence; do not lower the reserve or evict busy services to force the trio into memory.

## Out of scope

- Dedicated Qwen instance or endpoint for DIMON.
- Streaming or chunked TTS, changes to Qwen answer length, or speech-recognition hot-idling.
- Changes to stock research, order execution, account data, or non-Qwen language models.
- A guarantee that models remain resident indefinitely under external GPU pressure.
