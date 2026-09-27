# Dimon Gateway roadmap

**Status:** planning document. The working name is Dimon Gateway. This plan describes a synthetic, local-first hackathon demo; it must not connect to real bank accounts or execute real payments.

The build order is deliberate: first prove the smallest reliable action loop, then make that loop safe, then make it conversational and impressive.

## Winning focus after the core loop

The core local pipeline is now working end to end: Nemotron streaming → Qwen3.5
planning → deterministic Python policy and ledger execution → Kokoro response.
The next goal is to turn those working silos into one unmistakable Direct
Conversational Transaction Agent (DCTA) demonstration. The strongest claim is:

> Dimon can understand a messy banking request, decompose it into several actions,
> let the user correct it naturally, and prove that only the exact approved plan
> changed the ledger.

Priority order:

| Priority | Feature | Why it matters |
| --- | --- | --- |
| 1 | Heterogeneous action batch: bill payment + beneficiary transfer + internal transfer | Directly demonstrates a real personal-banking agent and matches the non-equity DCTA scenarios. |
| 2 | Receipt supersession after user correction | Shows that conversational changes invalidate stale receipts instead of executing an earlier interpretation. |
| 3 | Familiarity-aware review | Routine payments can receive a concise explanation while unusual payments clearly show why they need attention. |
| 4 | Voice-first polished flow | Makes the complete system feel like a direct conversational banking interface rather than a text form. |
| 5 | Attack demonstration | Makes the zero-trust boundary visible: injected instructions produce no ledger mutation. |
| 6 | Judge-facing metrics | Proves latency, field accuracy, replay rejection, and zero unauthorized writes with reproducible evidence. |

Equities, cross-border transfers, perfect phonetic matching, and real payment
connectivity remain optional extensions. A complete three-action synthetic flow
is more valuable than a broader but unreliable feature set.

## Current capability and gap map

| Layer | Current state | Next required proof |
| --- | --- | --- |
| Voice input | Nemotron streaming component works | Preserve transcript versions and measure name, amount, and account errors in the integrated flow. |
| Intent planning | Local Qwen3.5 produces bounded transfer and batch proposals | Add bill-payment and internal-transfer proposal contracts. |
| Grounding and policy | Python derives amounts, resolves identities, and rejects unsupported requests | Add independent semantic validation plus KYC, value, and velocity rules. |
| Approval | Stored receipts bind transfer facts and execute once | Add cryptographic user approval and supersede receipts after corrections. |
| Execution | SQLite commits balanced synthetic transfers atomically | Commit heterogeneous bill, beneficiary, and internal actions as one reviewed batch. |
| Spoken result | Kokoro can provide response audio | Generate speech only from the committed gateway result and record end-to-end latency. |
| Competition evidence | Brief and build plan are documented | Complete real CodeBuddy or WorkBuddy sessions and preserve the required history and screenshots. |

Use the [controlled CodeBuddy workflow](codebuddy/README.md) for the final row.
Its first task adds the read-only validation-result contract without granting
CodeBuddy access to the ledger or approval path.

The [next-work queue](next-work.md) records the implementation order and the
evidence required before the team marks each capability complete.

## Phase 0 — competition and evidence gates

The DBS banking brief has now been received and reviewed at
[`docs/competition handbook/tencent hackathon brief.pdf`](competition%20handbook/tencent%20hackathon%20brief.pdf).
It identifies the Direct Conversational Transaction Agent (DCTA) challenge and
requires a flexible conversational front end with a deterministic, zero-trust
execution boundary. Remaining gates are team eligibility, registration,
submission cutoff, previous-project reuse, and genuine CodeBuddy or WorkBuddy
evidence. Preserve the required tool history and screenshots while doing
original work; a GitHub fork alone does not settle eligibility.

The [CodeBuddy Contribution Controller](codebuddy/agent.md) prepares one bounded
work packet at a time. Prepared prompts and templates do not count as product
usage evidence. Only completed CodeBuddy or WorkBuddy sessions do.

**Exit evidence:** confirmed rules, an agreed disclosure of reused foundation if any, and an evidence checklist.

## Phase 1 — local structured-action spike

Run Qwen locally through `llama.cpp` / `llama-server`. Give it typed banking requests and require a constrained JSON action proposal or a clarification request. There is no ledger execution in this phase.

The question is not “can the model run Python?” It is whether a local model can reliably produce a small, valid action vocabulary that a server can inspect.

**Exit evidence:** saved test prompts and results showing valid proposals for ordinary requests, clarification for ambiguity, rejection of malformed values, and zero mock-ledger changes.

See [the local structured-action spike](spikes/local-structured-action.md).

## Phase 2 — contracts, receipts, and mock gateway

Implement the server-side contract boundary before a polished interface. The model may propose a draft; it cannot choose an arbitrary function, resolve a recipient ID, approve itself, or write the ledger.

Build a mock ledger with server-enforced ownership, balance and limit checks, canonical recipient resolution, immutable review receipts, expiry, nonces, idempotency, and atomic batch execution.

**Exit evidence:** an altered client amount, stale receipt, duplicate execution, and unapproved draft all fail at the gateway.

## Phase 3 — conversational transaction planning

Add multi-action parsing, clarification turns, independent read-only auditing, deterministic policy checks, and a review screen that displays the exact batch and total debit. The first complete hero plan should combine a bill payment, a beneficiary transfer, and an internal transfer. A guarded mock equity purchase is an optional later extension. A familiar payee or ordinary amount can reduce friction, but it never bypasses approval.

**Exit evidence:** the hero request produces a correct review receipt for a bill payment, a transfer to Mum, and a transfer to a linked joint account. The receipt is superseded when the user corrects one detail, and the approved batch executes once with no stale or replayed debit. Equity purchase evidence is optional.

## Phase 4 — voice conversation

Add push-to-talk transcription, the same text planning path, review on screen, and spoken results. Keep voice input and approval separate: speech may propose or confirm review navigation, but it cannot by itself authorize a debit.

**Exit evidence:** a recorded end-to-end demo from speech to reviewed mock execution and spoken confirmation.

## Phase 5 — adversarial safety and differentiation

Demonstrate the boundaries rather than merely claim them. Test hostile text embedded in a synthetic bill reference, receipt tampering, replay, ambiguous recipients, and balance changes after review. Put the optional semantic prompt-injection guard behind a flag and measure it against the core deterministic boundary.

After the core path works, evaluate the familiarity graph and experimental voice-identity work (wake word, speaker verification, noisy-audio handling, replay/clone detection). These are differentiators, not the authorization mechanism.

**Exit evidence:** an attack demo and an evaluation table with block rate, false refusals, transaction accuracy, and latency.

## Phase 6 — evaluation and submission package

Run the demo script repeatedly on a fixed synthetic dataset. Capture architecture, contract decisions, test results, limitations, user feedback, and the required CodeBuddy or WorkBuddy evidence. Prepare the cover image, short blurb, description, demo video or URL, and repository details.

**Exit evidence:** a rehearsed 3-minute demo and a complete submission checklist before the official deadline.

## Initial build sequence

1. Freeze the non-equity action catalog and contract shapes.
2. Complete the typed local-model spike.
3. Build draft, receipt, approval, and mock execution boundaries.
4. Add the heterogeneous review flow and receipt supersession.
5. Attach voice input and text-to-speech to the same planning path.
6. Add familiarity explanations, attack demonstrations, measurements, and only then optional research features.
