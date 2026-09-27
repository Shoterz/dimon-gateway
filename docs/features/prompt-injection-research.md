# Prompt-injection research

Status: research note; gateway controls remain the product safety boundary

## Direct answer

A larger or more capable model can be harder to inject, but no known parameter count or intelligence threshold makes prompt injection impossible. Fine-tuning can improve behaviour against attack families represented in training and evaluation data, but it is not a substitute for least privilege, deterministic validation, explicit approval, and server-side execution checks.

Prompt injection exploits the fact that language models receive instructions and untrusted content in the same context. The attacker tries to make the model treat untrusted content as higher-priority instruction. [OWASP's guidance](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html) describes direct, indirect, multi-turn, multimodal, retrieval, and agent-specific forms.

## What stronger models and training can do

| Approach | What it can improve | What it cannot prove |
| --- | --- | --- |
| More capable model | Better instruction following and contextual reasoning in tested cases | That every future adversarial instruction will be ignored |
| Instruction-hierarchy training | Helps the model prioritize system/developer instructions over untrusted tool or document content | A complete security boundary when the model can act on money or sensitive data |
| Targeted fine-tuning | Can reduce attack success on known attack distributions | Generalization to unseen attacks, obfuscation, multi-turn attacks, or new tools |
| Guardrail / auditor model | Adds another chance to flag suspicious content | Independence or reliability against the same adversarial context |
| Confirmation and least privilege | Limits the impact of a fooled model | That the user cannot be socially engineered into approving a bad receipt |

OpenAI reports that instruction-hierarchy training improves prompt-injection robustness on its evaluated benchmarks. It is evidence that targeted training can help, not evidence of immunity. [OpenAI instruction-hierarchy research](https://openai.com/index/instruction-hierarchy-challenge/).

## Current frontier-model evidence

Public system cards and safety announcements show improvement alongside continuing residual risk:

- GPT-5.6 evaluates direct and indirect prompt injection with automated red teaming; its system card explicitly treats results as directional and says some universal jailbreaks are likely to exist for any model. [GPT-5.6 system card](https://deploymentsafety.openai.com/gpt-5-6)
- OpenAI's Astra announcement reports a higher refusal rate on one cyber-jailbreak evaluation than GPT-5.6 Sol. That is a narrow evaluation result, not a claim that Astra is prompt-injection-proof. [Path to Astra](https://openai.com/index/path-to-astra/)
- Anthropic states that perfect jailbreak resistance is probably impossible today, reports ongoing red-teaming, and describes narrow jailbreaks as an expected residual risk even where no universal jailbreak has been found. [Fable 5 safeguard update](https://www.anthropic.com/news/redeploying-fable-5)

These sources do not establish a public, reproducible jailbreak for every named model. They establish the more important engineering conclusion: developers of frontier models still rely on layered defenses, red teaming, classifiers, permissions, and confirmations.

## Proposed layered intake defense

Use a deterministic gate before any LLM, then an optional semantic guard before the payment planner. This is feasible and worthwhile as defense in depth, provided that neither gate grants permission to execute.

```text
voice / typed text / bill reference
  → deterministic structural gate
  → semantic injection guard
  → payment planner
  → independent audit
  → deterministic receipt and policy checks
  → explicit approval
  → execution gateway
```

### Layer 0: deterministic structural gate

This layer does not try to understand every malicious sentence. It applies rules that are exact and cheap to test:

- enforce audio, transcript, field, and context-size limits;
- classify every inbound string by source, such as `user_request`, `bill_reference`, `payee_label`, or `tool_output`;
- reject malformed encodings, unsupported markup, unexpected control characters, and fields in the wrong schema position;
- resolve beneficiaries only through server-owned IDs, never model-provided account details;
- disallow the planner from receiving secrets, approval tokens, signing keys, or ledger-write tools; and
- preserve the original value and source for audit evidence.

Deterministic rules catch known bad structure and limit attack surface. They cannot reliably decide whether ordinary natural language is a hidden instruction.

### Layer 1: semantic injection guard

The guard receives untrusted content with its source label and returns a narrow structured assessment: `allow_for_planning`, `requires_clarification`, or `block`, plus categories such as policy-override attempt, approval-bypass attempt, hidden instruction, data-exfiltration request, or malformed content.

It has no tools, credentials, conversation memory beyond the request, or authority to alter recipients, amounts, or policies. If it blocks content, the planner receives a reason code rather than the raw blocked string. If it allows content, that only means “continue under normal safeguards,” never “this payment is safe.”

### Layer 2 and 3: planner and audit

The planner extracts a schema-constrained proposal. It does not execute actions. The independent auditor compares the transcript and clarification history with the complete draft and can block the creation of an approval-ready receipt.

Calling the same base model twice with two prompts is useful role separation, but not strong independence: one adversarial pattern can influence both calls. Stronger independence comes from different model families or a complementary deterministic verifier. For the first demo, separate contexts, different prompts, and deterministic gateway checks are realistic; do not claim that the two LLMs are fully independent.

## Resource and latency trade-off

Three sequential language-model passes—guard, planner, auditor—can noticeably increase voice-to-receipt latency. Keep the guard's input and output short, limit its context, and run it only on new or changed transcript versions. Record each stage's latency.

Recommended build order:

1. Establish deterministic structural checks, planner, independent audit, receipt binding, and gateway controls.
2. Measure the baseline against the adversarial cases already in this document.
3. Add the semantic guard behind a feature flag.
4. Compare blocked attacks, missed attacks, false blocks of valid payments, extra clarification, and total latency.

This makes the guard a measured safety improvement rather than a decorative extra model. If it creates many false blocks or excessive latency without reducing unsafe drafts, leave it out of the final demo and retain the deterministic authorization boundary.

## Relevance to Dimon Gateway

The critical attack is not merely a model producing bad text. It is a bad model output causing a financial action.

```text
untrusted transcript / bill reference / payee memo
  → planner may be influenced
  → draft is validated and audited
  → deterministic receipt is created
  → user explicitly approves that exact receipt
  → gateway rechecks binding and executes once
```

The planner must have no ledger-write credential, approval signing key, or unrestricted payment tool. The audit is read-only. The gateway resolves recipient IDs, validates money fields, checks ownership and current balance, verifies a short-lived one-time approval bound to the exact receipt, then performs the mock-ledger change atomically.

If a model is tricked into adding a payment to Eve, one of three safe outcomes should occur:

1. Validation or audit detects a mismatch or unknown field and blocks approval.
2. The receipt clearly shows the unexpected action and the user declines it.
3. If an attacker alters details after approval, receipt binding causes gateway rejection.

The remaining risk is social engineering: a user might approve a clearly displayed but malicious payment. Model robustness cannot eliminate that. Clear receipts, unfamiliar-payment warnings, limits, and deliberate confirmation reduce the chance and impact.

## Fine-tuning policy for the project

Do not fine-tune first. Establish a baseline with the selected Qwen model and a held-out suite of legitimate and adversarial requests. If the baseline is insufficient, run a clearly scoped instruction-hierarchy or structured-output experiment, then compare it against the unchanged baseline on unseen cases.

Report at least:

- attack success rate at the planner and auditor;
- false acceptance into an approval-ready draft;
- false refusal or excess clarification for legitimate payments;
- exact match for amount, recipient, source account, and currency;
- latency; and
- unauthorized mock-ledger debits, which must remain zero regardless of model result.

Fine-tuning is successful only if it improves a measured trade-off without weakening legitimate payment handling. It never relaxes gateway authorization policy.

## Test cases to retain

- Direct spoken instruction attempting to bypass approval.
- Malicious text in a synthetic bill reference.
- Prompt split across a payee label and memo.
- Encoded or typo-obfuscated instruction.
- Multi-turn attempt to establish a dangerous convention.
- Model output that tries to add an unrequested action.
- Receipt altered after user approval.
- Replayed approval.

Each test records the planner result, audit result, policy reason, receipt state, approval state, and ledger outcome. “Injection detected” is not the decisive metric; the decisive invariant is that it cannot produce an unauthorized debit.
