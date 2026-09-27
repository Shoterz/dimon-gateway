# Deferred next step: contracts and mock gateway

**Status:** parked until the interactive GUI and lightweight mock-ledger demo is complete.

The next technical phase after the interactive demo is to turn the conceptual
contract documents into executable, versioned JSON Schemas and build the
server-owned receipt, approval, and exactly-once execution boundary.

The local Qwen spike has already shown that:

- Qwen can extract a normal beneficiary transfer into constrained JSON;
- deterministic Python can request clarification for an ambiguous recipient;
- the tested capability-escalation wording cannot grant Python, approval, or
  ledger-write authority; and
- all current outcomes remain non-executing review drafts.

This is enough evidence to proceed beyond model-only feasibility, but it does
not establish that a reviewed transaction executes exactly once or that
tampering and replay fail.

## Target flow

```text
Qwen RawActionIntent
        ↓ untrusted
Gateway validation and identity resolution
        ↓
PaymentActionDraft
        ↓
Immutable ReviewReceipt
        ↓ explicit user approval
ApprovalRecord
        ↓
Atomic synthetic-ledger execution
        ↓
ExecutionResult
```

Only the gateway may resolve canonical IDs, create receipts, record approval,
or mutate the synthetic ledger. The model never receives a general Python,
shell, SQL, approval, or execution tool.

## First gateway milestone

Begin with `BENEFICIARY_TRANSFER` only:

1. A transfer intent proposes “Send John Smith S$50.”
2. The gateway resolves John Smith and savings to server-owned IDs.
3. It creates an immutable review receipt.
4. A simulated authenticated user approves that exact receipt.
5. The gateway debits savings and credits the synthetic destination exactly
   once.
6. A repeated execution returns the existing result without another debit.
7. A changed amount, changed recipient, missing approval, expired receipt, or
   replay attempt is rejected.

## Contract work still required

The documents in `docs/contracts/` are architectural sketches, not executable
contracts. Before the full gateway phase:

- create language-neutral, versioned schemas under `packages/contracts/`;
- reconcile the record names used by the groundwork and contract documents;
- replace placeholder batch actions with concrete schemas;
- define canonical serialization and receipt hashing;
- define receipt expiry, nonce consumption, and idempotency semantics;
- define the ledger schema and balanced-entry invariant; and
- define stable error codes and endpoint request/response shapes.

Use a discriminated union of action-specific intents rather than one large
object containing irrelevant nullable fields:

- `BeneficiaryTransferIntent`
- `InternalTransferIntent`
- `BillPaymentIntent`
- `EquityBuyIntent`

The model emits user-facing labels. The gateway resolves those labels to
canonical server-owned identifiers.

## Action expansion order

1. Beneficiary transfer
2. Internal transfer
3. Bill payment
4. Equity purchase
5. Multi-action atomic batch

Equity purchase comes last because it adds allow-listed instrument resolution,
price snapshots, quantity and fractional-share calculation, and allocation of
funds remaining after earlier actions. With foreign exchange deferred, the
first mock Apple instrument should use an explicitly synthetic SGD price.

## Prompt-injection position

Keep a small model evaluation set, but do not prioritize fine-tuning or a
sophisticated semantic injection classifier before the gateway boundary exists.
The decisive safety measurement is not whether an attack changes model text;
it is whether any unapproved or altered proposal can change the ledger.

## Completion evidence

The contracts-and-gateway phase is complete when automated tests demonstrate
that an altered client amount, stale receipt, duplicate execution, unapproved
draft, cross-user request, and insufficient balance all fail without an
unauthorized ledger change.

Related documents:

- [Roadmap](roadmap.md)
- [Initial action catalog](contracts/action-catalog.md)
- [Planned contract shapes](contracts/contract-shapes.md)
- [Local structured-action spike](spikes/local-structured-action.md)
