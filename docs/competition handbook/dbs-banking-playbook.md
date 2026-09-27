# IntentLock: banking-track competition blueprint

Status: reviewed competition blueprint, not a submitted entry. Updated 24 September 2026 after reviewing the uploaded DBS brief and official event page. This proposal follows the DBS challenge; do not reuse it for a separate sponsor track without reviewing that brief.

## Competition facts and submission gates

- Challenge: **Direct Conversational Transaction Agent (DCTA)**. Choose **Scenario 1: Voice-Enabled Payment and Transaction** and state that choice at the start of the presentation. The suggested workflow includes KYC and basic risk controls. The brief permits mock biometrics and transaction gateways; the feature list is guidance, not a demand to implement every feature.
- Original work must use at least one of **CodeBuddy or WorkBuddy**. Preserve genuine conversation history and **at least three screenshots** from development. Proof is an eligibility gate: without it, the project does not proceed to scoring. Codex work alone does not satisfy this requirement. Miora appears elsewhere in the brief, but the explicit basic requirement names CodeBuddy/WorkBuddy; satisfy the stricter requirement.
- Required: project title, blurb **under ten words**, description covering users/scenario/pain points/business and technical architecture/prompts/business value, tool conversation history, three chat screenshots, and a **16:9 cover**. Produce 1920×1080; the brief's suggested 380×216 is not exactly 16:9.
- Optional but useful: live demo URL (the brief says it earns bonus points), 5–8 minute demo video, and source repository. The banking section does not explicitly make a GitHub repository mandatory; provide one for reproducibility.
- Submission date: **16 October 2026**. Finalist announcement: **23 October**; top two teams per track. Demo Day: **3 November**, marked TBC in the PDF. Verify portal closing time/timezone and final event details before submitting. Internal submission target: 15 October.
- Team: **1–3 members, all based in Singapore**. Confirm eligibility and registration; neither is established by files in this workspace.
- The PDF gives ten general judging dimensions, ten points each. Detailed track-specific preliminary criteria arrive after registration. Its references to “game quality” and “regional” awards appear to be template text; do not assume those apply to banking. Monitor the portal for the banking-specific rubric.

Sources: uploaded `tencent hackathon brief.pdf`, pages 3–8 and 35–39; [official event page](https://tch.tencentcloud.com/contest/44). These are facts from the sources, not predictions of selection or a prize.

## Priority order for winning the DCTA demonstration

The working system already connects Nemotron streaming, Qwen3.5 planning,
deterministic Python policy and ledger execution, and Kokoro response audio. The
next work should integrate those silos into a single judge-facing story:

| Priority | Feature | Why it matters |
| --- | --- | --- |
| 1 | Heterogeneous action batch: bill payment + beneficiary transfer + internal transfer | Directly demonstrates a real personal-banking agent. |
| 2 | Receipt supersession after user correction | Shows safe conversational change handling rather than a second untrusted interpretation. |
| 3 | Familiarity-aware review | Routine payments get concise review; unusual payments explain why they need attention. |
| 4 | Voice-first polished flow | Makes the demonstration feel like a direct conversational interface. |
| 5 | Attack demonstration | Shows that hostile instructions cannot mutate the ledger. |
| 6 | Judge-facing metrics | Makes latency, accuracy, replay rejection, and zero unauthorized writes measurable. |

The strongest differentiator is not a larger model:

> Dimon can understand a messy banking request, decompose it into several actions,
> let the user correct it naturally, and prove that only the exact approved plan
> changed the ledger.

Equities, cross-border transfers, perfect phonetic matching, and real payment
connectivity remain optional extensions. The first complete hero flow should use
the three non-equity actions above.

## Recommended entry

**Title:** IntentLock

**Blurb:** “Speak your banking. Approve exactly what moves.” (Seven words.)

**Pitch:** “IntentLock turns a spoken banking request into a verified transaction receipt. AI drafts and clarifies; an independent auditor checks meaning; only explicit approval of that exact receipt can authorize the execution gateway.”

**Initial user:** a mobile-banking customer who understands their desired payments but finds repeated forms difficult, including people who benefit from voice or larger text. Validate that need through five consenting users; do not claim interviews or disability outcomes that have not happened.

**Hero request:** “From my savings, pay my electricity bill of 120 Singapore dollars, send Mum 500 Singapore dollars, and move 1,000 Singapore dollars to my joint account.”

One request produces three explicit actions and one review receipt. Synthetic opening balance: SGD 4,000; total debit: SGD 1,620; closing balance: SGD 2,380. Use fixed synthetic beneficiaries and a fake ledger throughout the hackathon.

**Competition hypothesis:** a memorable demonstration of convenient transactions plus independently enforced approval should address more judging dimensions than adding many loosely implemented features. This is a strategic recommendation, not knowledge of competitors or judges' preferences.

Alternatives considered:

1. **General financial assistant:** broad utility, but high scope and weak alignment with transaction execution. Drop it.
2. **Voice-only single payment:** feasible and accessible, but demonstrates little multi-intent reasoning or discrepancy handling. Use it as an early milestone.
3. **IntentLock with an attack walkthrough:** recommended. Keep a small complete transaction workflow and make its safety boundary inspectable.

Keep equities, cross-border FX, autonomous payee enrollment, and real-money integrations outside the initial build. They add suitability, pricing, identity, and dependency complexity. Demonstrate multiple intents using domestic payments and transfers. Treat “whatever is left” and “half of it” as clarification cases until their exact reference and limits are confirmed.

## What to build

1. Voice capture and text fallback, visible editable transcript, microphone consent, accessible controls. Never let speech recognition uncertainty turn into silent amount/currency/payee defaults.
2. Intent planner emitting schema-constrained drafts with source spans for each amount, payee, currency, and account. It can read synthetic metadata and propose actions; it cannot call execution APIs.
3. Short clarification for unresolved names, accounts, currencies, amount conflicts, or dependencies. “John” with two saved matches requires a selection. Conversation corrections create a new draft version.
4. Independent read-only audit, using the transcript **and explicit clarification history**, compared with the complete draft. Pass/fail/unknown per critical field, with evidence spans. Mismatch or unknown blocks approval. Separate calls and contexts; a second identical prompt is not meaningful independence. Shared transcription/model failure remains possible.
5. Deterministic review receipt: source account, verified beneficiary and masked identifier, amount, currency, fee, execution order, total debit, remaining balance, expiry, and batch identifier. Render from the immutable server receipt, not model-generated HTML. Escape untrusted text.
6. Approval service and execution gateway with separately scoped credentials. Model services have no ledger write credentials, signing keys, or approval capability. The authenticated customer approves the exact receipt; conversational “yes” never substitutes for this step.
7. Mock KYC/account ownership check, saved-payee checks, balance validation, configurable value/velocity limits, secondary approval for high-risk cases, and an auditable result for every action. Show failures and safe recovery, not just the happy path.

Use CodeBuddy for actual implementation/debugging and keep evidence from day one. Tencent ASR and agent orchestration are useful additions if accounts and supported deployment regions are available; they are not substitutes for the required development-tool proof. Verify API capabilities and accent support on real samples before claiming them. Do not add WorkBuddy or cloud components solely to decorate the architecture.

Use the repository's [controlled CodeBuddy workflow](../codebuddy/README.md) to
prepare and review those contributions. The first work packet gives CodeBuddy a
strict read-only validation contract. It cannot touch execution, approval,
receipts, or the ledger. Preserve the real conversation and test evidence; the
prepared prompt alone does not satisfy the submission requirement.

## Trust boundary and transaction rules

Pipeline: **voice/text → transcript and clarifications → draft planner + read-only auditor → deterministic checks → immutable receipt → explicit approval → execution gateway → mock ledger**.

The approval path is separate from the planner. A malicious or mistaken model must be able to propose a bad draft without gaining authority to execute it. A semantic firewall is defense in depth; it is not the security boundary. OWASP documents that prompt injection cannot be assumed solved by prompts or another guardrail model: [prompt injection risk](https://genai.owasp.org/llmrisk/llm01-prompt-injection/), [prevention guidance](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html).

**Receipt binding:** validate the schema, then use a tested canonical serializer such as [RFC 8785 JCS](https://www.rfc-editor.org/rfc/rfc8785). Use integer minor units for money with a currency scale registry and enforce safe numeric bounds. Bind user/account ownership, exact destination IDs and display details, amounts/currencies/fees, action order, batch mode, policy version, receipt version, expiry, and unique operation IDs. Any material edit invalidates approval and starts review again.

**Authorization options:** the hackathon can use a plainly labeled mock biometric decision with a separate mock approval service. That demonstrates workflow, not possession of a biometric credential. A stronger browser prototype can use verified WebAuthn assertions: generate a fresh random server challenge, store it against the immutable receipt digest, authenticated user, expiry, and one-time approval record, then verify signature, expected challenge, RP ID, origin, credential ownership, user presence, and required user verification on the server. The gateway must load that same receipt and recompute its digest before execution. Never trust a client-supplied `approved: true` or verification result. [WebAuthn specification](https://www.w3.org/TR/webauthn-3/).

WebAuthn user verification may use a PIN or biometrics, and it does not itself display banking amounts to the user. A web review screen is not a native bank-grade secure confirmation overlay. Describe those limits accurately. The assertion provides evidence of credential use; do not claim it proves informed consent or legal non-repudiation in every deployment.

**Execution:** under a database transaction, verify ownership, approval binding/expiry, current balance, risk state, unconsumed nonce, and idempotency key; atomically reserve/consume authorization and write results. A retry returns its recorded outcome rather than debiting again. Concurrency tests must demonstrate this. Recheck value/velocity limits at execution, not only when planning. Timeout or validator failure means no approval, not automatic success.

**Batch behavior:** make the synthetic ledger all-or-none inside one database transaction. Show which request failed, with zero batch debit. Do not promise this across real external payment APIs. A production adapter would need durable per-action state, reservations where available, reconciliation, and a receipt showing partial results; a completed external transfer may not be reversible. Dependent actions must stop when prerequisites fail.

**Audit/privacy:** link transcript version, clarification events, draft versions, field evidence, policy outcomes, receipt digest, approval event, and ledger outcome by interaction ID. Record structured reasons rather than hidden model chain-of-thought. Use synthetic data in the demo. For a deployment, minimize stored audio, define retention/access rules, encrypt sensitive records, and obtain recording consent. A hash chain is tamper-evident only relative to a trusted retained checkpoint; it is not inherently tamper-proof.

## Six-minute demonstration

- **0:00–0:30:** identify DBS DCTA, Scenario 1, target customer, and the repeated-form problem. Show a measured synthetic baseline, not an invented DBS app benchmark.
- **0:30–1:45:** speak the three-action request. Show transcript, structured draft, field evidence, and fixed receipt. Approve explicitly. The fake ledger records SGD 1,620 once.
- **1:45–2:20:** “Send fifty to John.” Show two saved Johns and ask a precise question; no debit while unresolved. Currency/source ambiguities also need resolution.
- **2:20–3:00:** put an adversarial instruction inside a synthetic bill reference. Even if it distorts a draft, no execution authority appears. Show rejection reason and unchanged balance.
- **3:00–3:40:** substitute SGD 5,000 for SGD 500 **after approval**. Gateway rejects a receipt-binding mismatch. This is a direct API test, not an animation pretending to be security.
- **3:40–4:15:** replay the previously authorized request. No second debit. Show recorded original outcome and replay/idempotency reason.
- **4:15–4:50:** force a semantic discrepancy, then a balance/risk failure. Explain which independent layer rejects each case. Dependent batch remains untouched.
- **4:50–5:30:** show the trust-boundary diagram and brief audit trail. State exactly which components are live, mocked, or deferred.
- **5:30–6:00:** show measured usability/evaluation results, genuine CodeBuddy evidence, and pilot scope. Closing line: “Every executed transaction matches an explicitly approved receipt.” Label that as the invariant demonstrated in the prototype, not universal proof of bank-grade safety.

Keep a prerecorded fallback clearly labeled as prerecorded. A local fixture mode should be clearly labeled and must never be presented as live ASR or LLM output. Do not silently swap in predetermined results when a model call fails.

## Evidence to earn the published judging points

| Published dimension | Concrete evidence to prepare |
| --- | --- |
| Impact & Relevance | Same three-action task measured against a small deterministic form baseline; clear customer problem. |
| Human-Centered Design | Five user sessions, observed confusion, and changes made from feedback. |
| AI Interaction | Live multi-intent extraction, targeted clarification, independent discrepancy check, source-field evidence. |
| Technical Execution | Server-side authorization, direct API rejection tests, replay/concurrency behavior, and reproducible setup. |
| Feasibility | Small pilot with synthetic ledger; real adapter contract and honest biometric/settlement limitations. |
| Demo & Storytelling | Six-minute rehearsed arc: easy request, ambiguity, attack, enforced boundary, result. |
| Innovation & Creativity | Editable intent-to-receipt trace plus an inspectable approval binding; explain differentiation without claiming invention of signatures or zero trust. |
| UX & Accessibility | Text fallback, keyboard flow, large readable confirmation, explicit recording state, screen-reader labels. |
| Responsible AI & Ethics | No model write authority, abstention on missing facts, consent/data minimization, documented residual risks. |
| Overall Quality & Judge's Impression | Consistent visual identity, working deployment, truthful evidence, and concise limitations. |

These are preparation choices, not projected scores. Re-map when the actual banking preliminary rubric arrives.

## Evaluation contract

Pre-register a held-out set before tuning: **60 valid multi-intent requests, 20 ambiguous/correction requests, 20 malicious-context requests**. Add realistic speech samples with different speakers; report sample sizes and per-field errors. Keep exact known demo scripts separate from held-out evaluation.

Track task completion time, successful completion, extra clarification turns, amount/payee/account/currency exact match, incorrect approval-ready drafts, validator false accepts/refusals, ASR field errors, and p50/p95 latency. Report voice and text separately. Five users are exploratory usability evidence, not population-level proof. Record actual numbers; no prefilled “accuracy” percentages or savings claims.

Release-blocking gateway checks:

- Unsigned request, forged approval boolean, wrong user's credential, altered destination/amount/currency/fee/account/order, expired approval, and reused authorization cannot create an unauthorized debit.
- Sequential and concurrent retries cause at most one debit for the same approved operation; same idempotency key with different content is rejected.
- Missing KYC/account ownership, unresolved payee, unapproved receipt version, audit failure, and failed/out-of-band risk approval block execution.
- Negative/zero/overflow amounts, unsupported currencies, insufficient funds, current-balance races, and velocity-limit races have explicit deterministic outcomes.
- Under the declared mock atomic batch mode, any failed action produces zero batch debit.

Target zero unauthorized debits in this finite test suite. Passing does not prove every attack is impossible. Keep adversarial detection rates separate from gateway authorization outcomes: an injection can bypass a semantic filter while the authorization boundary still holds.

## Build schedule

- **18–21 September:** confirm sponsor/eligibility/registration/handbook, get Tencent accounts, perform user interviews, freeze synthetic scenario and schema, preserve first genuine tool screenshots.
- **22–27 September:** ship text-to-draft, clarifications, fixed receipt, separate approval service, deterministic gateway, and atomic fake ledger. Make direct gateway rejection tests pass before polishing UI.
- **28 September–4 October:** add real model calls/auditor and voice; evaluate held-out samples and repair field errors. Add risk controls, audit view, and replay/concurrency checks.
- **5–10 October:** run five user sessions and accessibility checks; measure baseline comparison, refine receipt clarity, and validate deployment.
- **11–14 October:** freeze scope, rehearse demo, record optional video, prepare cover, description, repository/setup, genuine chat screenshots and history.
- **15 October:** submit after a complete portal preview and requirement check. Do not wait for 16 October to discover missing evidence.

If capacity slips: cut TTS, equities, cross-border transfers, and extra orchestration first. Preserve complete text workflow, a demonstrable voice path, honest audit, and server-enforced approval. No sponsor-facing outreach or submission has been made.

## Better prompts for actual CodeBuddy development

Use these in real development sessions; they are prompts, not product-usage evidence until executed and recorded.

**1 — architecture and schema:**

> Build the DBS DCTA Scenario 1 synthetic prototype described in this blueprint. First identify trust boundaries and propose a strict transaction/receipt schema. Planner and auditor must have read-only credentials. Only a separate execution gateway may write the fake ledger after approval of an immutable receipt. Treat payee labels and bill references as untrusted data. No real-money APIs. Explain residual risks and mocks before coding.

**2 — gateway first:**

> Implement the deterministic approval-binding and fake-ledger gateway. Money uses validated integer minor units. Enforce authenticated account ownership, immutable receipt digest, expiry, current balance, risk limits, one-time authorization and idempotency atomically. Write meaningful direct API tests for unsigned calls, field edits after approval, replay, concurrent retries, balance races and failed atomic batches. Do not treat an LLM verdict or client approval boolean as authority.

**3 — interpretation and audit:**

> Add a schema-constrained planner and independently prompted read-only auditor. Compare drafts with transcript and clarification history, preserving source evidence for critical fields. Ask about unresolved payees, accounts, currencies, amounts and references. Unknown, mismatch, malformed response or timeout must block approval. Record actual held-out evaluation results; do not manufacture model accuracy.

**4 — demo and interface:**

> Add microphone consent, visible transcript, keyboard/text fallback and an accessible deterministic receipt. Never render model HTML. Clearly label mocks and offline fixtures. Build the six-minute hero and attack demo using actual gateway responses. Capture genuine development evidence and document reproducible run instructions and limitations.

## What the “make no mistakes” posts actually show

Two useful first-person accounts were found. They are self-reported experiences, not controlled evidence that a phrase wins competitions.

- [Jaseel Kt's robotics hackathon account](https://www.linkedin.com/posts/jaseelkt_robotics-ai-physicalai-activity-7475561357048733697-UAPQ): the desired learned policy was unavailable; modular, self-checking actions and a deterministic fallback made the live robot demonstration work. Transferable lesson: engineer boundaries and observable outcomes; budget failure paths.
- [Eric Zhang's Statle account](https://www.linkedin.com/posts/ericzhangdeveloper_sasehacks-ai-hackathon-activity-7450170820292677632-jAp4): he attributes a sponsor-category win to replacing a generic polished interface with a deliberate visual identity and a clearer plan. Transferable lesson: choose a specific product point of view and show it consistently.

Neither account establishes that “make no mistakes” caused a win. For this banking entry, use clear invariants, bounded scope, real tests, genuine sponsor-tool evidence, and a distinctive approval receipt.

## Still required before this becomes an entry

Sponsor clarification; registered eligible team; detailed banking rubric; actual prototype; live model/voice integrations or honestly labeled mocks; measured evaluations; genuine CodeBuddy/WorkBuddy usage; cover; description; demo assets; final portal submission. No victory, security certification, interviews, scores, product usage, or submission is claimed.
