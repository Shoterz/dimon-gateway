# Inference Providers and Fixtures Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every teammate a deterministic, model-free development loop while preserving one configuration boundary for fixture, remote, and host-local inference.

**Architecture:** Introduce typed inference providers at the application composition root. Existing Qwen, Nemotron, and Kokoro clients become the `local` provider; deterministic repository fixtures become the default `fixture` provider; the `remote` provider interface is reserved here and implemented by the inference-gateway plan. Business policy, receipts, approvals, and SQLite mutation remain local and identical in every mode.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, unittest, browser JavaScript, JSON fixtures.

**Spec:** [Team repository and inference architecture design](../specs/2026-09-27-team-repository-and-inference-architecture-design.md)

## Global Constraints

- This plan depends on the repository-foundation plan.
- `fixture` is the teammate default and must require no network, model, CUDA, Docker, or external process.
- `local` reuses configured host endpoints; it must never start or copy model weights.
- `remote` must fail at startup with a typed configuration error until the gateway client lands.
- All three modes feed the same deterministic grounding, policy, receipt, approval, and SQLite code.
- Fixture mismatch must fail closed with `FIXTURE_NO_MATCH`; it must never invent a response.

## Review Focus

- Changing `DIMON_INFERENCE_MODE` must change only provider wiring.
- Fixtures must cover safety and failure cases as well as the happy path.
- Voice and TTS fixtures must use valid bounded audio formats and deterministic outputs.
- The UI must make fixture mode visible so a recorded demo cannot be mistaken for live inference.
- Existing injected fakes in unit tests must continue to work.

---

### Task 1: Define the provider composition boundary

**Files:**
- Create: `apps/dimon_live/inference.py`
- Create: `apps/dimon_live/provider_factory.py`
- Modify: `apps/dimon_live/app.py`
- Modify: `apps/dimon_live/model_adapter.py`
- Test: `apps/dimon_live/tests/test_inference_modes.py`

**Interfaces:**
- `InferenceMode = Literal["fixture", "remote", "local"]`.
- `InferenceProviders(mode, extractor, recognizer, voice_lifecycle, synthesizer)` is an immutable dataclass.
- `build_inference_providers(mode: str, env: Mapping[str, str]) -> InferenceProviders` validates configuration once at startup.
- `InferenceFailure(reason_code: str, message: str)` is the common typed provider failure; `FixtureNoMatch` uses reason code `FIXTURE_NO_MATCH`.
- `create_app(..., providers: InferenceProviders | None = None)` preserves the existing explicit dependency parameters for focused tests and rejects conflicting injection.

- [ ] Write failing tests for the three accepted modes, an unknown mode, missing local endpoints, the remote-mode placeholder error, and conflicting provider injection.
- [ ] Run `uv run python -m unittest apps.dimon_live.tests.test_inference_modes -v`; confirm the provider API is absent.
- [ ] Add the provider types and startup factory.
- [ ] Move environment-driven Qwen/Nemotron/Kokoro construction out of route setup and into the factory without changing their client implementations.
- [ ] Teach `ActionService` and voice/speech boundaries to propagate stable `InferenceFailure.reason_code` values without exposing exception text to the browser.
- [ ] Run the focused tests and the full existing application suite.
- [ ] Commit the provider boundary as `refactor: centralize inference providers`.

### Task 2: Add versioned structured-intent fixtures

**Files:**
- Create: `apps/dimon_live/fixture_inference.py`
- Create: `fixtures/inference/manifest.json`
- Create: `fixtures/inference/intent/exact-beneficiary.json`
- Create: `fixtures/inference/intent/ambiguous-beneficiary.json`
- Create: `fixtures/inference/intent/missing-amount.json`
- Create: `fixtures/inference/intent/paynow-exact.json`
- Create: `fixtures/inference/intent/paynow-unknown.json`
- Create: `fixtures/inference/intent/saved-beneficiary-batch.json`
- Create: `fixtures/inference/intent/prompt-injection.json`
- Create: `fixtures/inference/intent/malformed-output.json`
- Create: `fixtures/inference/intent/qwen-timeout.json`
- Test: `apps/dimon_live/tests/test_fixture_inference.py`

**Interfaces:**
- `FixtureCatalog.load(path) -> FixtureCatalog` validates one manifest version and unique case IDs.
- `FixtureExtractor.extract(text) -> ExtractedIntent` matches normalized input exactly and returns the checked-in raw model output plus parsed intent.
- Special fixture outcomes raise typed `InferenceFailure` values such as `UPSTREAM_TIMEOUT` and `INVALID_MODEL_OUTPUT`.

- [ ] Write failing tests for every required intent fixture, duplicate normalized inputs, unsupported manifest versions, schema-invalid fixture files, and an unmatched prompt.
- [ ] Run the focused test and observe the missing catalog.
- [ ] Add a manifest with `fixture_contract_version: 1`, human-readable case IDs, exact normalized trigger text, outcome kind, and relative payload path.
- [ ] Encode fixture outputs against the existing action-intent schema; preserve adversarial text as input data rather than executable prompt instructions.
- [ ] Implement exact normalized matching with no fuzzy fallback and return `FIXTURE_NO_MATCH` for unknown inputs.
- [ ] Run the focused fixture tests and the existing service/adversarial suites.
- [ ] Commit the intent catalog as `feat: add deterministic intent fixtures`.

### Task 3: Add deterministic voice and speech fixtures

**Files:**
- Modify: `apps/dimon_live/fixture_inference.py`
- Create: `fixtures/inference/voice/manifest.json`
- Create: `fixtures/inference/speech/manifest.json`
- Test: `apps/dimon_live/tests/test_fixture_voice_speech.py`

**Interfaces:**
- `FixtureRecognizer.open_stream(on_partial)` implements the current recognizer protocol and emits a configured transcript after a valid audio commit.
- `FixtureVoiceLifecycle` can produce normal, `VOICE_BUSY`, and `UPSTREAM_UNAVAILABLE` outcomes by named fixture case.
- `FixtureSynthesizer(text) -> bytes` returns a valid deterministic mono WAV tone and can emit `UPSTREAM_UNAVAILABLE`.
- `DIMON_FIXTURE_VOICE_CASE` and `DIMON_FIXTURE_SPEECH_CASE` select cases, defaulting to normal operation.

- [ ] Write failing tests for a final transcript, partial transcript ordering, valid WAV headers, deterministic bytes, Nemotron unavailable/busy, and Kokoro unavailable.
- [ ] Run the focused test and observe failures.
- [ ] Implement in-memory recognizer streams and generate a short bounded PCM WAV through Python's standard `wave` module; do not add binary blobs to Git.
- [ ] Enforce the same audio size, duration, and event-order contracts used by live providers.
- [ ] Run the voice/speech fixture tests plus the existing voice and speech suites.
- [ ] Commit the media fixtures as `feat: add deterministic voice fixtures`.

### Task 4: Make fixture mode the documented teammate default

**Files:**
- Modify: `apps/dimon_live/cli.py`
- Modify: `apps/dimon_live/app.py`
- Modify: `apps/dimon_live/static/index.html`
- Modify: `apps/dimon_live/static/app.js`
- Modify: `apps/dimon_live/static/app.css`
- Modify: `.env.example`
- Modify: `README.md`
- Test: `apps/dimon_live/tests/test_fixture_ui.py`

**Interfaces:**
- Console command: `dimon-dev`, equivalent to `DIMON_INFERENCE_MODE=fixture dimon-serve` unless the user explicitly selects another mode.
- `GET /api/session` includes `inference_mode` with one of the three public mode names.
- The UI renders a persistent `Fixture inference` badge when the session reports fixture mode.

- [ ] Write failing tests for the `dimon-dev` default, session metadata, and visible fixture badge.
- [ ] Run the focused tests and observe failures.
- [ ] Add the command and expose only the non-sensitive mode name in session metadata.
- [ ] Add the badge and fixture-case instructions without exposing upstream URLs or environment values.
- [ ] Update the quick start with exact fixture prompts and explain that policy and SQLite execution are real local code even when model output is fixed.
- [ ] Run all Python tests and the existing static-client tests.
- [ ] Commit the teammate default as `feat: make fixture mode the development default`.

### Task 5: Add a mode-parity contract suite

**Files:**
- Create: `apps/dimon_live/tests/test_provider_contract.py`
- Create: `docs/development.md`

**Interfaces:**
- One provider contract suite accepts a provider factory and verifies intent, voice, speech, error, timeout, and cancellation behavior independent of implementation.

- [ ] Write the contract suite against fixture providers first.
- [ ] Adapt existing local provider fakes to the same suite so the test proves interface parity without requiring live models.
- [ ] Document when to use fixture, remote, and local modes, including the exact environment variables and expected failure messages.
- [ ] Run the provider contract, API, service, voice, speech, and adversarial tests together.
- [ ] Commit the fixture-mode checkpoint after repository-boundary checks pass.

### Verification Contract

Before reporting this checkpoint complete, run:

```bash
uv run python -m unittest apps.dimon_live.tests.test_inference_modes -v
uv run python -m unittest apps.dimon_live.tests.test_fixture_inference -v
uv run python -m unittest apps.dimon_live.tests.test_fixture_voice_speech -v
uv run python -m unittest apps.dimon_live.tests.test_provider_contract -v
uv run python -m unittest discover -s apps/dimon_live/tests -p 'test_*.py'
scripts/check-repository-boundary
git diff --check
```

Start `uv run dimon-dev` with all model services stopped. Submit each documented fixture prompt, record one voice fixture, retrieve one WAV artifact, and confirm an unknown prompt returns `FIXTURE_NO_MATCH` without mutating SQLite.
