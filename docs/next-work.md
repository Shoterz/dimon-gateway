# Dimon next-work queue

Use this file to select the next implementation task. Update each status only
after the named evidence exists.

## Working foundation

The project already has these components:

- Nemotron streaming voice input
- Local Qwen3.5 structured-intent planning
- Deterministic Python policy and execution
- Synthetic SQLite balances and ledger entries
- Immutable transfer receipts and exactly-once execution
- Kokoro spoken output
- Saved-beneficiary batches and synthetic PayNow resolution

## Current task

Run the prepared
[strict validation-result contract](codebuddy/first-contribution.md) task in a
real CodeBuddy session. Preserve the conversation, screenshots, diff, and test
output with the [evidence template](codebuddy/evidence-log-template.md).

This first contribution creates the bounded contract for the independent
validation agent. It does not connect a second model or affect execution.

## Ordered implementation queue

### 1. Independent validation agent

Connect a separate read-only validator that compares the transcript and
clarification history with the proposed transaction. Require `MATCH` for each
critical field. `MISMATCH`, `UNKNOWN`, malformed output, and timeout must block
approval.

**Completion evidence:** focused mismatch and timeout tests, stored validation
records, and zero ledger writes for blocked decisions.

### 2. Correction-driven receipt supersession

Assign versions to transcripts, drafts, validations, and receipts. A user
correction must create a new version and make each earlier receipt impossible
to approve.

**Completion evidence:** changing one amount or recipient invalidates the old
receipt, while the replacement receipt contains the corrected facts.

### 3. Cryptographic or mock-biometric approval

Bind user approval to the stored receipt digest, authenticated user, expiry,
and one-time challenge. Prefer WebAuthn where the demo machine supports it. Use
a labelled mock-biometric service as the fallback permitted by the brief.

**Completion evidence:** altered, expired, wrong-user, and replayed approvals
fail without a debit.

### 4. Mock KYC, ownership, value, and velocity policy

Add deterministic account ownership and synthetic KYC status. Enforce amount
limits, request-rate limits, and a secondary-review state for high-value
actions.

**Completion evidence:** each policy has an allow and block case with a stable
reason code. The gateway rechecks the policy during execution.

### 5. Bill-payment and internal-transfer contracts

Add `BILL_PAYMENT` and `INTERNAL_TRANSFER` proposal, resolution, receipt, and
ledger shapes. Resolve billers and linked accounts from server-owned synthetic
directories.

**Completion evidence:** each action completes alone, asks for clarification
when a destination is ambiguous, and refuses unsupported identifiers.

### 6. Heterogeneous atomic batches

Combine a bill payment, beneficiary transfer, and internal transfer in one
review receipt. Show each action, the total debit, and the remaining balance.
Commit all actions or none of them under the declared synthetic batch model.

**Completion evidence:** the hero request creates one exact receipt; one failed
action leaves every balance unchanged; approval executes the batch once.

### 7. Unified audio-to-ledger trace

Link the audio reference, transcript version, Qwen draft, validator decision,
policy result, receipt digest, approval event, ledger transaction, and Kokoro
response with one interaction ID. Keep raw developer records out of the normal
customer interface.

**Completion evidence:** one recorded voice request can be reconstructed from
input through committed result without relying on hidden model reasoning.

### 8. Attack demonstration and measured results

Run the fixed legitimate, ambiguous, correction, injection, tampering, replay,
and velocity cases. Record field accuracy, clarification rate, validator false
accepts and refusals, unauthorized writes, and latency by stage.

**Completion evidence:** a reproducible results table and a rehearsed attack
demo showing the unchanged ledger after rejection.

### 9. CodeBuddy evidence and submission assets

Collect genuine CodeBuddy or WorkBuddy history throughout the queue rather than
waiting until submission week. Keep at least three clear screenshots, session
records, and the corresponding reviewed diffs. Prepare the cover, project
description, demo video, repository instructions, and live link if available.

**Completion evidence:** every required portal item has a real file or link,
and the team can trace each CodeBuddy screenshot to a reviewed contribution.

## Scope held for later

- Equity purchase
- Cross-border transfers and foreign exchange
- Real PayNow, bank, brokerage, or identity integration
- Perfect phonetic matching for arbitrary names
- Voice biometrics presented as bank-grade authentication
