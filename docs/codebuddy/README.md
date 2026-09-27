# Controlled CodeBuddy contribution workflow

This directory prepares small, genuine CodeBuddy contributions for the DBS DCTA
entry. A controller agent writes the work packet. The user then runs that packet
inside CodeBuddy and preserves the original conversation and test output.

The controller does not write product code, manufacture chat records, or claim
that a planned session occurred. CodeBuddy usage counts as evidence only after a
real session changes the project and the team records the session.

## Current system state

| Capability | State | Remaining work |
| --- | --- | --- |
| Nemotron streaming voice input | Working component | Connect one recorded voice session to the versioned transaction trace and measure transcription errors. |
| Qwen3.5 local planner | Working | Extend the proposal contract from beneficiary transfers to bill payments and internal transfers. |
| Deterministic Python gateway | Working | Add the independent validation decision, risk policy, and signed approval gates required by the brief. |
| Synthetic SQLite ledger | Working | Extend atomic execution to heterogeneous action batches. |
| Immutable transfer receipts | Working | Supersede stale receipts after a conversational correction and bind validator and approval records. |
| Kokoro spoken response | Working component | Read only the committed gateway result and capture end-to-end latency. |
| Saved-beneficiary batch | Working | Support bill payment plus beneficiary and internal transfers in one receipt. |
| PayNow mock resolution | Working | Keep it synthetic and outside the first heterogeneous batch. |

## Missing track requirements

The competition brief names these capabilities that the current prototype does
not yet complete:

- An independent read-only validation agent that compares each draft with the
  raw transcript and clarification history.
- Cryptographic user approval bound to the exact receipt. A WebAuthn flow or a
  labelled mock biometric service can demonstrate this boundary.
- Mock KYC and account ownership checks plus amount and velocity limits.
- Bill-payment and internal-transfer contracts in a heterogeneous action batch.
- Draft and receipt versioning when the user corrects a request.
- One interaction trace linking audio, transcript, draft, validation decision,
  approval, and ledger result.
- A rehearsed attack demonstration and measured field accuracy, latency,
  replay rejection, and unauthorized-write results.
- Genuine CodeBuddy or WorkBuddy history and the submission screenshots.

## Workflow

1. Give the controller agent one candidate feature and the current repository
   state.
2. The controller selects the smallest complete contribution that supports a
   named brief requirement.
3. The controller fills in [`prompt-template.md`](prompt-template.md) with exact
   files, constraints, tests, and stop conditions.
4. Run the prompt in CodeBuddy without rewriting its contribution as your own.
5. Save the unedited conversation and at least one screenshot from that session.
6. Review the diff, run the named tests outside CodeBuddy, and record the result
   with [`evidence-log-template.md`](evidence-log-template.md).
7. Accept the contribution only when it stays inside scope and the tests pass.

The first prepared work packet is
[`first-contribution.md`](first-contribution.md). It gives CodeBuddy a meaningful
part of the independent validation boundary without access to receipts,
approval, execution, or the ledger.

## Scope rules

- One behavior change per session.
- No more than three product files unless the user approves a larger scope.
- No package installation, schema migration, broad refactor, or infrastructure
  change unless the work packet names it.
- No removal or weakening of tests, validation, receipt binding, idempotency, or
  fail-closed behavior.
- No secrets, real banking data, real payment APIs, or production credentials.
- No claim that CodeBuddy produced evidence until the real session and review
  have occurred.
