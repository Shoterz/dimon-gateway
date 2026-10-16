# `backend-edits` Capability Map

This document records the behavior added on the team's `backend-edits` branch
at commit `8258ef6`. Each section names the implementation and tests that back
the claim. The branch uses synthetic accounts and investments; no real money
moves.

For identity, face verification, device signing, and mock Singpass details,
see [Identity and Transaction Authorization](identity-and-transaction-authorization.md).

## Runtime and model inventory

The branch keeps three inference modes:

| Mode | Intent model | Speech-to-text | Speech output | Intended use |
| --- | --- | --- | --- | --- |
| `fixture` | Recorded JSON cases | Recorded transcript | Recorded output | Tests and quick demos |
| `local` on Linux GPU host | Qwen 3.5 9B Q5 through llama.cpp | NVIDIA Nemotron streaming ASR by default | Kokoro | Existing team GPU-host path |
| `local` on Windows | Same Qwen family through llama.cpp Vulkan or CPU | whisper.cpp | Kokoro on CPU | New one-click local path |
| `remote` | Same bounded services behind the inference gateway | Remote transcription endpoint | Remote speech endpoint | Team integration |

The branch does not replace Qwen with a new intent model. It adds alternative
speech recognition backends:

- `whisper_cpp`: vendor-neutral Windows/local transcription;
- `whisper_intel`: OpenVINO path for Intel Arc;
- `nemotron`: the existing Linux GPU-host default.

It also adds YuNet and SFace ONNX models for face controls. These are separate
from Qwen, Nemotron, and Kokoro. Model selection lives in
`apps/dimon_live/provider_factory.py`; paths and timeouts live in
`.env.example`.

## Voice-to-action latency path

```text
microphone frames
  -> browser resamples to 16 kHz
  -> server waits for endpoint silence (default 1.4 s)
  -> STT backend performs final decode
  -> transcript_final fills the Run Intention field
  -> Qwen extracts bounded JSON intent
  -> deterministic grounding, validation, policy, and receipt creation
  -> result renders
```

If text is slow to appear in the input, measure endpoint detection and the STT
final decode. Qwen runs after `transcript_final`, so it affects the later
result delay. `apps/dimon_live/voice.py` implements endpointing and streaming;
`services/whisper_cpp/server.py` performs background partial decodes and a
final full-buffer decode; `model_adapter.py` records Qwen `latency_ms`.

## Banking and conversational features

### Simulated investing

**Behavior.** Users can buy and sell fictional stocks, ETFs, a REIT, and a
bond ETF; create or stop recurring plans; and view cash, holdings, performance,
and order history.

**Implementation.** `investing.py` parses orders with deterministic rules,
creates immutable digest-bound receipts, and applies suitability and price
checks. `market.py` defines ten fictional instruments and deterministic price
history. `database.py` settles cash and holdings together with double-entry
investment records. `static/invest_ui.mjs` supplies the UI.

**Evidence and limits.** `test_investing.py` covers buy, sell, plans, limits,
price protection, authorization, and atomic settlement. Prices and instruments
are simulated; the feature is not financial advice or brokerage integration.

### Trending picks and long-term plans

**Behavior.** The interface ranks fictional instruments by one- and
three-month momentum, shows dividend candidates, and offers plan templates
matched to a four-question risk profile. Every suggestion lists strengths and
risks.

**Implementation.** `market.py` calculates snapshots and history;
`investing.py` derives rankings, profiles, plan projections, and suitability.
The logic is rule-based and does not ask the language model for investment
advice.

**Evidence.** `test_investing.py` covers profile requirements, recommendation
shape, projections, risk acknowledgement, and plan behavior.

### Singlish, nicknames, and conversational amounts

**Behavior.** Phrases such as “pay Alicia ten bucks” and “transfer my mum
fifty for the makan” resolve amounts and known aliases. Number words only count
inside payment slots, avoiding counts such as “for 2 cakes” or “3 times.”

**Implementation.** `grounding.py` extracts deterministic amount evidence;
`payees.py` resolves names, nicknames, and history; `validation_agent.py`
independently re-parses the original text.

**Evidence.** `test_everyday_speech.py` exercises Singlish, bare amounts,
nicknames, false amount contexts, and ambiguous language.

### Multiple payments in one sentence

**Behavior.** A request can produce two to four payment rows under one batch
receipt and one approval.

**Implementation.** `model_adapter.py` permits a bounded batch intent without
model-supplied amounts. `service.py` recovers recipients and derives every
amount from the original text. `batch_receipts.py` and `database.py` bind and
settle the batch atomically.

**Evidence.** `test_everyday_speech.py`, `test_batch_grounding.py`, and
`test_batch_receipts.py` cover ordering, amounts, ambiguity, and all-or-nothing
settlement.

### PayNow lookup and saved payees

**Behavior.** A known mobile proxy resolves to a masked synthetic recipient.
Unknown proxies are refused. After settlement, the user can save the recipient
under a nickname; new-payee controls remain active for 24 hours.

**Implementation.** `paynow.py` owns proxy lookup; `payees.py` owns nickname
and history rules; `database.py` persists the beneficiary after an eligible
settlement.

**Evidence.** `test_paynow.py` and `test_read_back_and_payees.py` cover known
and unknown proxies, eligibility, duplicate names, and later nickname use.

### Better same-name disambiguation

**Behavior.** Ambiguous choices show full names, account endings, and recent
payment history. Partial or phonetic matches never auto-execute.

**Implementation.** `name_resolution.py` produces candidates;
`payees.py` supplies history; `service.py` creates clarification state and
requires the exact response contract.

**Evidence.** `test_recipient_resolution_eval.py`, `test_service.py`, and
`test_read_back_and_payees.py` cover exact, ambiguous, close, and stale
clarifications.

## Security controls

### Scam detection and holds

**Behavior.** The gateway holds requests containing common scam narratives,
including an authority demanding a transfer to a “safe account,” secrecy,
threats, job or investment scams, and remote-access requests. It returns a
warning and does not create a draft.

**Implementation.** `scam_guard.py` normalizes the text and evaluates fixed
phrase and pattern groups. `service.py` calls it before model extraction. The
guard can block or hold a request; it cannot approve one.

**Evidence.** `test_scam_guard.py` covers scam patterns, benign near-matches,
the warning response, and proof that the extractor is not called on a hold.

**Limit.** This is a deterministic demo rule set, not a complete fraud model.
Attackers can invent wording outside its patterns.

### Prompt-injection firewall

**Behavior.** The gateway rejects text that tries to change system rules,
bypass approval, request tools or secrets, smuggle role markup, hide commands
with Unicode controls, or embed encoded payloads. Oversized input is also
rejected before Qwen sees it.

**Implementation.** `input_firewall.py` performs normalization and
deterministic category checks. `model_adapter.py` wraps allowed text in
escaped `<untrusted_user_request>` tags and tells Qwen to treat the contents as
data. `service.py` runs the firewall for typed text, voice transcripts, and
clarification replies.

**Evidence.** `test_input_firewall.py` and `test_adversarial_safety.py` cover
attack corpora, hidden characters, encoded input, benign trigger-adjacent
phrases, and the challenge brief's injection example.

**Limit.** The firewall reduces known attack forms. The independent validator
and signed execution boundary remain necessary because no text filter proves a
model safe.

### Independent draft validation

**Behavior.** A second deterministic component compares the proposed receipt
with the user's original words and the server-owned payee directory. A mismatch
freezes the receipt; an unverifiable field appears for explicit confirmation.

**Implementation.** `validation_agent.py` re-parses currency, source, amount,
recipient, and PayNow evidence without trusting the model output.
`validation_store.py` records results. `database.py` repeats the validation
inside settlement, after challenge-time validation.

**Evidence.** `test_validation_agent.py` covers amount, recipient, source,
currency, PayNow, batch, and corrupted-draft discrepancies.

### Risk-based approval tiers

**Behavior.** Controls increase with transaction risk:

| Condition | Additional requirement |
| --- | --- |
| Saved payee under S$200 | Face and device signature |
| S$200 to S$2,000 | Spoken read-back |
| New payee or over S$2,000 | One-time hardware-token code |
| Unusual amount | Explicit acknowledgement and read-back |
| New payee in first 24 hours | S$100 per-transfer cap |

All transfers also face a S$5,000 per-transfer and S$10,000 daily synthetic
limit.

**Implementation.** `risk_policy.py` calculates requirements from KYC,
amount, history, payee age, and recent activity. `approval_gateway.py` and
`database.py` enforce the decision at challenge issuance and settlement.

**Evidence.** `test_risk_policy.py` covers boundaries, KYC, cumulative value,
new payees, unusual amounts, and re-checks during execution.

### Spoken transaction read-back

**Behavior.** Before higher-risk approval, the system speaks a sentence built
from the same review data shown on screen. The sign action waits for successful
playback.

**Implementation.** `read_back.py` builds text from server receipt fields;
`speech.py` queues and stores audio; `static/app.js` polls and plays it before
enabling approval. Kokoro is the default. `tencent_tts.py` can call Tencent
Cloud and falls back to Kokoro, then browser speech.

**Evidence.** `test_read_back_and_payees.py`, `test_api.py`, and
`test_ui_client.py` cover wording, thresholds, pending audio, playback gating,
and fallback behavior.

### Hardware-token simulation

**Behavior.** New-payee and large-value transfers require a six-digit code
shown in a separate mock-token window. The code applies to one receipt.

**Implementation.** `risk_policy.py` decides when the token is required.
`approval_gateway.py` derives the code with an HMAC over the receipt digest
and enforces attempt limits. `static/mock_token.html` displays the synthetic
token.

**Evidence.** `test_risk_policy.py`, `test_api.py`, and
`test_signed_approval.py` cover missing, wrong, locked, cross-receipt, and
valid codes.

### History, unusual amounts, and velocity limits

**Behavior.** The gateway shows prior payments during disambiguation, flags an
amount over three times the largest of at least two past payments, pauses after
more than five transfers in ten minutes, and applies a tighter small-transfer
limit.

**Implementation.** `payees.py` combines seeded and ledger history;
`risk_policy.py` calculates unusual-amount and rolling-window results;
`database.py` queries committed transactions again inside settlement.

**Evidence.** `test_risk_policy.py` and `test_read_back_and_payees.py` cover
thresholds, history, unusual amounts, velocity, and concurrency-safe rechecks.

### Append-only hash-chained audit records

**Behavior.** The system records input, model draft, validator result,
receipts, challenges, signatures, face results, refusals, and settlement. Each
event incorporates the prior event hash. SQLite triggers refuse update and
delete operations.

**Implementation.** `audit_log.py` canonicalizes each payload, computes the
chain hash, creates append-only triggers, verifies continuity, and builds
receipt evidence bundles. Voice evidence stores an audio hash and optional WAV
outside the ledger.

**Evidence.** `test_audit_log.py` covers valid chains, edits, deletion,
re-hashing attempts, reset history, and evidence bundles.

**Limit.** A local administrator who can replace the database and all external
copies remains outside this demo's trust model. Export or remote anchoring
would strengthen production evidence.

## Demo and operating features

### Presenter view

`static/presenter.html` and `static/presenter.js` show the phone interface next
to a plain-language live audit feed, with scenario and attack buttons. The
server exposes `/presenter`. `test_ui_client.py` and `test_api.py` verify the
route and expected controls.

### Plain-language settlement summaries

`static/app.js` renders who paid whom, amount, approval method, and reference
from the server's settlement result. It no longer displays reconstructed SQL.
`test_ui_client.py` checks the copy and avoids database implementation detail.

### Optional Tencent Cloud speech

`tencent_tts.py` signs Tencent requests with TC3-HMAC-SHA256. `speech.py`
selects the provider and falls back to local Kokoro; the browser provides the
last fallback. This optional path sends read-back text to Tencent Cloud.
`test_api.py` and the speech tests cover selection and fallback.

### Windows installation and lifecycle tooling

`install.cmd` and `scripts/windows/install.ps1` check Windows version, CPU,
RAM, disk, GPU, and Vulkan; install the locked Python environment; download
pinned runtimes and models with SHA-256 verification; and write local paths to
`.env`. `start.cmd` starts Qwen, whisper.cpp, Kokoro, and the app, handles port
conflicts, warms the model, and records logs. `stop.cmd` stops recorded process
trees. These scripts target Windows PowerShell 5.1 and are not the Linux setup
path.

## Pop!_OS setup boundaries

The one-click branch installer targets Windows. On Pop!_OS:

1. Use `uv sync --locked --group dev` for the application environment.
2. Run fixture mode first; it needs no Qwen, ASR, or Kokoro weights.
3. Install the YuNet and SFace files before real face enrollment.
4. For live local inference, use the inherited Linux GPU-host services and
   `run.sh` launchers documented in `docs/gpu-host-setup.md` and
   `docs/inference-gateway.md`.
5. Open the application through `http://localhost:<port>` so platform WebAuthn
   remains eligible.

### Current machine setup recorded on 2026-10-02

The isolated checkout is `.worktrees/backend-edits` on branch
`test/backend-edits`. Its locked environment is installed and its ignored
`apps/dimon_live/.env` selects local mode on port 8892.

The Pop!_OS host has an RTX 4070 with 12 GB VRAM and NVIDIA driver 580.159.03.
The existing local supervisor already exposes the required
`qwen35-9b-abliterated-q5` model on port 8080, Kokoro responds on port 3001,
and the on-demand Nemotron control socket exists at
`/run/user/1000/jarvis-nemotron.ctl.sock`. No replacement Qwen, Nemotron, or
Kokoro download is required for this shared setup.

Start the configured branch with:

```bash
cd <repository-checkout>
DIMON_PORT=8892 .venv/bin/dimon-serve
```

The explicit `DIMON_PORT` is necessary because the CLI chooses its listening
port before `app.py` loads `apps/dimon_live/.env`. Then open
`http://localhost:8892`.

The local-mode smoke test reported `inference_mode: local` and healthy app
startup. `/api/face` reported `engine_ready: false`, which is expected until
the YuNet and SFace files are installed. The app process was stopped after the
smoke test, so the command above still needs to be run for interactive use.

## Later `backend-edits` work on the consolidated UI

The hands-free, voiceprint, multi-request, payee-addition, TRTC, and app-lock
work that followed `8258ef6` now runs on this branch's banking UI and policy.

| Amount | Approval (consolidated policy) | What the voiceprint adds |
| --- | --- | --- |
| Under S$200 | Spoken "yes, confirm" + device signature | A match against the registered voiceprint satisfies the spoken step for real (`VOICEPRINT_AND_DEVICE_SIGNATURE`); the mock phrase still works without a voiceprint |
| S$200 and above | Singpass + face + device signature | Nothing: voice never replaces Singpass or the face |

A signature from the hands-free voice-approval key is accepted only together
with a voice match for that exact challenge and receipt.

Where it lives in the UI:

- **Ask tab:** the "Hey Dimon" hands-free toggle and voice registration sit
  under the Speak button. Hands-free requests and results appear in the same
  conversation thread.
- **Several requests in one sentence** (`multi_intent.py`) render as a list
  with one review per request. Cross-domain bill + transfer + stock requests
  still use the consolidated multi-action review, which takes precedence.
- **Topbar:** the bell counts Singpass authorisations waiting; the log-out
  button locks the app until the registered face unlocks it.
- **Pay tab:** contacts show nicknames, the last payment, and a "New" flag
  from `GET /api/payees`.

Two `backend-edits` additions follow this branch's product instead:

- **One account by default.** The second "everyday" account, and the
  "which account pays?" question it causes, is opt-in with
  `DIMON_EVERYDAY_ACCOUNT=1`.
- **No simulated US stocks.** NVIDIA, Apple, and other real companies go to
  the real-stock flow with live quotes. The simulated catalog keeps only
  fictional instruments.

The TRTC explainer panel is not shown, because the Ask tab is the assistant.
TRTC speech recognition and synthesis remain selectable through
`DIMON_STT_BACKEND=trtc` and `DIMON_TTS_PROVIDER=trtc`.
