# Dimon Gateway documentation

Dimon Gateway is a conversational-banking research demo. Nemotron supplies
streaming voice input, local Qwen3.5 proposes structured transaction intent,
deterministic Python owns policy and the synthetic SQLite ledger, and Kokoro
speaks committed results. The models receive no approval or ledger authority.

## Runnable prototype

- [Dimon Live runbook](../apps/dimon_live/README.md) — start and demonstrate the
  localhost UI
- [Guarded-autopilot design](superpowers/specs/2026-09-22-guarded-autopilot-prototype-design.md)
- [Implementation plan](superpowers/plans/2026-09-22-guarded-autopilot-prototype.md)
- [Deferred next step: contracts and gateway](deferred-next-step-contracts-and-gateway.md)
- [Controlled CodeBuddy workflow](codebuddy/README.md) — prepares bounded,
  genuine contributions and submission evidence
- [Next-work queue](next-work.md) — ordered remaining work and completion
  evidence
- [Runtime and service catalog](runtime-service-catalog.md) — living inventory
  of application modules, host inference dependencies, deployment boundaries,
  and planned components
- [Team repository and inference architecture](superpowers/specs/2026-09-27-team-repository-and-inference-architecture-design.md)
  — proposed Git publication boundary, development modes, inference gateway,
  GPU-host reuse, and Cloudflare environments
- [Team repository implementation program](superpowers/plans/2026-09-27-team-repository-program.md)
  — ordered execution gates for repository setup, fixtures, the inference
  gateway, GPU-host reproducibility, Cloudflare routing, and GitHub publication

The prototype auto-executes only an exact, funded transfer to a saved synthetic
beneficiary. Ambiguous, unsupported, invalid, and underfunded requests do not
mutate the ledger. This “magic” path is a demonstration policy, not a claim that
real banking should omit explicit confirmation.

Saved-beneficiary batches, receipt-bound confirmation, phonetic suggestions,
and synthetic PayNow lookup are also implemented. The next gaps are an
independent validation engine, signed user authorization, risk and velocity
rules, heterogeneous bill/beneficiary/internal actions, correction-driven
receipt supersession, and one measured voice-to-receipt demonstration. The
[roadmap](roadmap.md) tracks this work.

## Earlier research

- [Roadmap and phases](roadmap.md)
- [Initial action catalog](contracts/action-catalog.md)
- [Action contract shapes](contracts/contract-shapes.md)
- [Local structured-action spike](spikes/local-structured-action.md)
- [Groundwork plan](groundwork-plan.md)
- [Transaction familiarity](features/transaction-familiarity.md)
- [Relationship proximity](features/relationship-proximity.md)
- [Approval and friction](features/approval-and-friction.md)
- [Voice identity and noisy audio](features/voice-identity-and-noisy-audio.md)
- [Prompt-injection research](features/prompt-injection-research.md)
- [Idea backlog](features/idea-backlog.md)
- [DBS competition blueprint](competition%20handbook/dbs-banking-playbook.md)
