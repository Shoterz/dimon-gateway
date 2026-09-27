# Feature idea backlog

Status: raw ideas, not commitments

## Now: prove the core

- Voice request with visible transcript and typed fallback.
- Structured payment proposal from Qwen through llama.cpp.
- Seeded mock ledger with recipient identity resolution.
- Standard, attention, and enhanced review states.
- Explicit approval bound to exact payment details.
- Familiarity reasons based on direct, confirmed history.
- Demonstration of a changed-detail and approval-replay rejection.

## Next: make the risk story memorable

- Synthetic relationship graph with direct, two-hop, and three-hop examples.
- A small visual explanation of why a proposed payment is familiar or unfamiliar.
- Counterparty labels such as “Mum” with an underlying immutable recipient ID.
- Recurring-payment cadence detection.
- A comparison screen showing an ordinary repeat payment and a large unfamiliar payment.

## Later: only after the core works

- Multiple languages and voices.
- Continuous conversation or interruption handling.
- Personalized amount ranges and user-adjustable sensitivity.
- Fine-tuning experiment measured against unseen legitimate and adversarial requests.
- Real financial integrations, only if permitted and appropriate for the event.

## Questions to answer before implementation

- What currency and how many seeded recipients will the demo use?
- What makes an amount “large” in the demo scenario?
- Will the relationship graph be synthetic for the pitch, or derived only from the user's own mock ledger?
- Does enhanced review mean a second deliberate click, re-entering the amount, or both?
- How will the demo make mock data obvious to judges?
