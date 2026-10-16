# Cross-Domain Multi-Action Request Design

**Date:** 2026-10-07

**Status:** Draft for review
**Scope:** One natural-language request containing a supported bill payment,
saved-beneficiary transfer, and stock purchase, presented as one grouped review.

## Purpose

Dimon currently routes a request through separate bill, transfer, and stock
handlers. When one sentence names actions in more than one domain, the first
handler that accepts the sentence can consume it. In the reported request, the
stock handler returned a quote for NVIDIA and the bill and dinner transfer were
silently omitted.

This change lets Dimon identify multiple supported actions in one request,
prepare all of them, and present them together for review. Existing domain
approval rules remain authoritative for every action.

## Success criteria

1. A request naming a supported bill, a saved-recipient transfer, and a stock
   purchase produces a grouped response containing all three actions.
2. Every action is grounded to a distinct span of the user's original text.
   Model-produced values cannot replace missing evidence or change amounts.
3. The grouped review shows each action's destination, amount or quantity,
   funding source, and any domain-specific terms needed for approval.
4. Bill, transfer, and stock actions continue through their existing review,
   device-authentication, and execution paths. Merely submitting the combined
   prompt creates no transaction or order fill.
5. If an action is unsupported, ambiguous, or cannot be grounded or prepared,
   Dimon explains which action needs clarification and does not silently omit
   it or prepare only the actions that happened to parse.
6. Single-domain requests and the existing saved-beneficiary batch flow remain
   backward compatible.

## User-facing flow

For a request such as:

> Pay my PUB Water bill, send Alicia 45 dollars for dinner, and buy two shares
> of NVIDIA at market using my investment account.

Dimon identifies each action, grounds it to the transcript, looks up any
required server-owned data, and returns one grouped review with bill, transfer,
and stock sections. Bill amount may use a supported synthetic statement total
when the user asks to pay the whole/outstanding bill and does not state an
amount. The transfer must resolve to a saved beneficiary and have an amount.
The stock section uses the existing verified quote and order-review flow.

The user reviews and approves each action using that action's existing controls.
Bill and transfer receipts retain their device-authentication requirements;
stock approval retains its order-specific Singpass, face, and device checks.
The grouped presentation is not one cross-domain atomic transaction: an action
approved by the user may complete while another remains unapproved or later
fails. The UI must show each action's state independently. The friend-transfer
and utility-bill amounts in one request still contribute together to the
payment authentication tier, as required by the transaction-authentication
policy. The stronger resulting tier applies to the grouped payment approvals;
the stock order keeps its existing separate approval policy. Atomicity applies
to saving the review group and its child receipts only, not to later approval
or execution across domains.

## Proposed architecture

Add a bounded multi-action planning boundary before the current single-domain
dispatch. Qwen returns an ordered list of supported action candidates and exact
evidence spans. The model has no tools, IDs, approval authority, or execution
authority.

```text
Original request
      |
      v
Multi-action extractor (bounded schema, ordered candidates + evidence)
      |
      v
Deterministic grounding against original text and server directories
      |
      +--> utility bill preparation
      +--> saved-recipient transfer preparation
      +--> verified stock quote/order preparation
      |
      v
Grouped response with domain-owned review items
      |
      +--> existing bill approval path
      +--> existing transfer approval path
      +--> existing stock approval path
```

The planner only coordinates extraction, grounding, and presentation. Each
domain adapter remains responsible for validation, immutable receipt data,
approval, and commit. Existing handlers are invoked with the relevant grounded
clause or typed candidate; they must not reinterpret the complete multi-action
sentence independently.

Before creating any pending review records, the coordinator validates and
prepares every candidate. If one candidate cannot be prepared, it returns a
clarification/block response identifying that action, and creates no review
records for the request. When all are ready, the grouped response contains all
domain-specific review objects and a parent group identifier. Creating the
group and all of its child review records is one database transaction, so a
storage failure cannot leave a partial group. Approvals remain independent and
idempotent through existing endpoints.

## Grounding and failure behavior

- Candidate order follows occurrence order in the user's transcript.
- Each candidate must include an evidence substring present in the original
  transcript. Exact amount and quantity evidence is re-parsed server-side.
- Billers and beneficiaries resolve only through their existing server-owned
  directories and aliases.
- Stock instrument, side, quantity, order type, and funding source must pass
  the existing stock grounding and quote rules.
- Ambiguous or unsupported actions block preparation of the entire group and
  name the missing detail. The user can restate or remove that action.
- Extra action text that the planner cannot classify must not be discarded;
  the request is blocked for clarification.
- No model response can approve, sign, skip a challenge, modify a receipt, or
  directly change a balance or portfolio.

## UI behavior

Render one grouped result in the Ask transcript with distinct subcards for
each action. Each subcard uses its existing domain labels and approval button.
Show a visible per-action state (`Needs review`, `Approved`, `Completed`, or
`Not completed`) so sequential approvals and failures are understandable. Do
not report the group as fully complete until all actions have completed.

If preparation needs clarification, keep the user in the conversation and
identify the relevant action. Do not display partial prepared cards in that
case.

## Non-goals

- One cryptographic signature or one financial execution transaction across
  bill, person-to-person transfer, and equity subsystems.
- Atomic rollback across independently approved domain actions.
- Automatic approval or execution from language-model output.
- Arbitrary action/tool plans, more than the supported domains, or more than
  the existing per-domain item limits.
- Replacing the current single-action or saved-beneficiary batch contracts.
- Repairing the separate intermittent Nemotron transcription failure; it is a
  runtime issue to diagnose independently.

## Verification scenarios

1. Bill + transfer + stock request yields all three review sections.
2. A missing transfer amount blocks the entire group and creates no receipts.
3. An unknown recipient or unsupported biller is never guessed.
4. Model evidence that is not present in the original request is rejected.
5. Approving one grouped item completes only that item; remaining items stay
   pending and are shown accurately.
6. Replaying an approval cannot execute an item twice.
7. Existing bill-only, transfer-only, stock-only, and transfer-batch flows
   preserve their current outcomes.
