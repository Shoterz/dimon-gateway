# Backend-on-Consolidated Merge Design

## Goal

Merge `origin/backend-on-consolidated` into the product represented by
`origin/consolidated-ui` without losing either branch's working features. The
result keeps the safety-first UI and its recovery behavior, adds the teammate's
backend and voice features, and sends all speech through server-managed
providers.

## Branch Facts

- Merge base: `bfc0abfece24609d125a3d21714c5e91262aff2d`
- UI tip: `645ebf60396793353ca0433d9e3d911cbc6a89f6`
- Backend tip: `461d65bf32914e0caf9e10b30a4478b8fdcacb73`
- The UI side contains 25 unique commits.
- The backend side contains 6 unique commits and changes 89 files.
- Git reports content conflicts in six files:
  - `apps/dimon_live/service.py`
  - `apps/dimon_live/static/app.js`
  - `apps/dimon_live/static/index.html`
  - `apps/dimon_live/static/invest_ui.mjs`
  - `apps/dimon_live/static/paper_equity_ui.mjs`
  - `apps/dimon_live/tests/test_database.py`

## Merge Strategy

Create an isolated integration branch from `origin/consolidated-ui`, then merge
`origin/backend-on-consolidated` with a merge commit. Preserve both parent
histories. Resolve each conflict by behavior instead of choosing one side for
the whole file.

Use the consolidated UI as the presentation and recovery foundation. Add the
backend branch's controls where they fit the current information hierarchy.
Keep non-conflicting backend files from the teammate branch unless tests or
the contracts below expose an incompatibility.

## Conflict Decisions

### Service routing

Keep the consolidated branch's synthetic-investment precedence so catalog
requests cannot fall into the real-market stock path. Add the backend branch's
rule-based stock purchase and sale parser, payee-addition flow, and model
fallback behavior. Route in this order:

1. Existing safety, bill-payment, and multi-action checks.
2. Synthetic catalog and plan requests.
3. Deterministic real-stock sale, research, and purchase parsing.
4. Model-assisted real-stock handling.
5. Payee addition, balance inquiry, and transfer handling.

Tests must pin requests that could match both synthetic and real-stock
vocabularies.

### Main client integration

Keep the consolidated branch's evidence lifecycle, diagnostic separation,
startup watchdog, accessible controls, audio-artifact player, navigation, and
transaction formatting. Integrate these backend-branch behaviors:

- hands-free wake word and queued voice requests;
- voiceprint registration and voice-confirmed low-risk actions;
- session lock and face unlock;
- multi-request status and progress;
- payee additions;
- stock sales, pending orders, settlement notices, and cancellation;
- top-bar authorization notifications.

One state owner must coordinate push-to-talk, hands-free capture, audio
playback, dialogs, and app lock. The client must not open two microphones or
submit the same request through two voice controllers.

### HTML shell

Keep the consolidated branch's page structure, favicon, synthetic-demo label,
accessibility fixes, and safety-first wording. Add lock, logout, notification,
hands-free, identity, and voice-registration controls inside that structure.
Keep the synthetic marker visible after adding the top-bar actions.

### Investment UI

Keep the consolidated branch's sector classes, accessible labels, failure
states, SGD and funding-source formatting. Add share quantities, sell actions,
pending-order cards, settlement updates, proceeds, and cancellation. Render
sell proceeds with the server-provided funding currency and fee values.

### Database seed test

Expect the union of both valid seed additions: utility settlement accounts
from the consolidated branch and the masked payee account from the backend
branch. Preserve the consolidated branch's updated savings balance.

## Speech Provider Contract

The primary speech path uses the existing configured provider stack:

- The tracked remote mode uses the authenticated inference gateway for
  transcription and speech generation. That gateway represents the shared
  Whisper and Kokoro deployment.
- Local mode defaults to `whisper_cpp` for transcription and Kokoro for speech
  generation. An explicit `whisper_intel` setting remains valid.
- Fixture mode stays deterministic and never calls a cloud backup.

TRTC acts as an automatic backup when its credentials exist. Users do not
select it in the interface. The application must attempt TRTC only after the
primary provider reports a connection failure, timeout, or unavailable
upstream. Invalid audio, size limits, rejected input, policy decisions, and
schema errors must return their original error and must not trigger TRTC.

The speech-recognition adapter must retain a bounded copy of each utterance.
If the primary stream fails for an allowed availability reason, the adapter
replays that utterance to TRTC once. It must not run the providers in parallel.
The speech-synthesis adapter calls the primary synthesizer first and calls
TRTC once after an allowed availability failure.

Remove `window.speechSynthesis` and `SpeechSynthesisUtterance` from shipped
client code. Hands-free prompts and replies request a server-side audio
artifact and play it through the existing audio path. A failed primary and
failed backup produces an explicit unavailable state and leaves the microphone
ready for retry or typed input.

Retain the TRTC implementation and its unit tests. Do not expose the standalone
TRTC conversational assistant as an alternate voice mode. The merged product
uses TRTC only through the fallback adapters.

## Backend Integration

Keep the teammate branch's non-conflicting modules for app lock, hands-free
coordination, voiceprints, multi-intent requests, payee additions, deterministic
stock parsing, order lifecycle, session cleanup, and scale controls.

Preserve the current branch's bounded inference, receipt, evidence, recovery,
and exactly-once contracts. Auto-merged shared files require semantic review,
with extra attention to `app.py`, `database.py`, `paper_equity.py`, session
management, and speech setup.

The app must serve each new browser module through an explicit static route.
The startup watchdog must fire only when session, state, and payee loading
cannot establish a usable shell. A locked session may load only the resources
and endpoints needed to show and complete face unlock.

## Error Handling

- Record which provider handled each speech request without exposing secrets.
- Preserve stable public reason codes from the primary provider.
- Record primary and backup availability failures in private diagnostics.
- Stop after one TRTC attempt per utterance or synthesis request.
- Keep hands-free capture paused while Dimon audio plays.
- Restore typed input and retry controls when both speech providers fail.
- Keep all financial actions behind the existing deterministic validation and
  signed approval boundary, regardless of transcription provider.

## Verification

Add tests before changing speech fallback behavior. The tests must prove:

- primary recognition and synthesis succeed without contacting TRTC;
- an allowed primary availability failure calls TRTC once;
- validation, audio-limit, and policy failures do not call TRTC;
- two provider failures return the expected unavailable state;
- fixture mode never configures TRTC;
- shipped JavaScript contains no browser speech synthesis API;
- hands-free prompts use server audio artifacts;
- push-to-talk and hands-free capture do not overlap;
- app lock blocks account and action APIs until face unlock;
- synthetic investing keeps precedence over real-stock parsing;
- real-stock buy, sell, pending, cancel, and settlement paths work;
- the database seed includes both branches' accounts.

Run the focused voice, UI, service, investing, database, app-lock,
multi-intent, stock-order, TRTC, and session tests. Then run the full test suite.
Run syntax checks for every changed JavaScript module and start the fixture app
for a browser smoke test of Home, Ask, Wealth, lock/unlock, hands-free failure,
and order review flows.

## Out of Scope

- Redesigning the safety-first UI.
- Replacing the current inference gateway or its credentials.
- Sending financial actions through the standalone TRTC assistant.
- Adding a browser speech fallback.
- Changing the product from synthetic banking and paper trading to live money
  or brokerage execution.

## Acceptance Criteria

The merge preserves both branch histories and leaves no conflict markers. The
current UI remains recognizable, while the teammate features work inside it.
Whisper and Kokoro handle normal speech traffic. TRTC receives traffic only
after an eligible primary outage. Browser-native synthesis does not ship. All
focused checks and the full test suite pass, or the final report names each
pre-existing failure with its evidence.
