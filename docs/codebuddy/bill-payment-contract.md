# CodeBuddy contribution: strict bill-payment proposal contract

You are contributing one bounded, test-first feature to the DIMON synthetic
DBS Direct Conversational Transaction Agent demo.

## Outcome

Create a dependency-free, read-only Python contract that parses an untrusted
model proposal for a `BILL_PAYMENT`. This contribution defines proposal shape
only. It must not resolve a real biller, calculate or move money, create a
receipt, request approval, or connect to execution.

## Read before editing

- `apps/dimon_live/action_intent.schema.json`
- `apps/dimon_live/model_adapter.py`
- `apps/dimon_live/tests/test_model_adapter.py`
- `docs/competition handbook/dbs-banking-playbook.md`

You may inspect other files for context, but you may create or change only:

- `apps/dimon_live/bill_payment_contract.py`
- `apps/dimon_live/tests/test_bill_payment_contract.py`

## Contract shape

The parser accepts a mapping with exactly these keys:

- `contract_version`: exactly integer `1`; booleans are not integers here.
- `kind`: exactly `BILL_PAYMENT`.
- `biller_label`: a non-empty string proposed from the user's words.
- `biller_evidence`: a non-empty verbatim evidence string.
- `amount_evidence`: a non-empty verbatim evidence string. Do not accept a
  model-generated numeric amount or minor-unit value.
- `currency`: exactly `SGD` for this first synthetic contract.
- `currency_evidence`: a non-empty verbatim evidence string.
- `source_account_label`: a non-empty string or `null`.
- `source_account_evidence`: a non-empty string or `null`.
- `requested_execution_date`: an ISO `YYYY-MM-DD` string or `null`.
- `execution_date_evidence`: a non-empty string or `null`.

The source-account label and evidence must either both be strings or both be
`null`. The execution date and its evidence must either both be strings or both
be `null`. Reject invalid calendar dates.

Return an immutable value object. Expose one specific contract exception for
all malformed input. Reject missing keys, extra keys, empty strings, wrong
types, unsupported currency, invalid dates, and any approval-, receipt-,
credential-, canonical-ID-, execution-, tool-, shell-, SQL-, or ledger-shaped
key by virtue of the exact-key contract.

This task validates structure only. Do not claim that an evidence string is
grounded in a transcript; deterministic transcript grounding will be a later
integration task.

## Constraints

- Write the focused tests first and run them once to show the expected failure
  before creating the implementation.
- Use only the Python standard library and immutable data objects.
- Do not edit an existing file.
- Do not install a dependency or change runtime configuration.
- Do not import or call database, service, model, receipt, approval, identity,
  execution, or ledger modules.
- Do not add SQL, shell access, tool calls, credentials, canonical biller IDs,
  receipt digests, approval fields, or ledger authority.
- Do not weaken, delete, or rewrite an existing test.
- Stop and ask before proceeding if another file or broader authority appears
  necessary.

## Required tests

Cover at least:

1. A complete proposal produces the expected immutable value.
2. Optional source-account fields may both be `null`.
3. Optional execution-date fields may both be `null`.
4. Missing and extra keys fail.
5. Empty or non-string evidence fails.
6. A source-account label/evidence mismatch fails.
7. An execution-date/evidence mismatch fails.
8. Invalid dates fail.
9. Non-SGD currency fails.
10. Approval-, receipt-, execution-, tool-, SQL-, and ledger-shaped extra keys
    fail.
11. The production module does not import any DIMON database, service, model,
    receipt, approval, identity, execution, or ledger module.

Run:

```bash
uv run python -m unittest apps.dimon_live.tests.test_bill_payment_contract -v
uv run python -m unittest apps.dimon_live.tests.test_model_adapter -v
```

The wider application suite currently has thirteen known baseline failures in
the legacy fictional-investing tests because those company phrases now enter
the real-stock resolver. Do not modify those tests or route around those
failures in this contribution.

## Working method

Before editing, state your short plan and confirm that it fits the two allowed
files. Then:

1. Write focused tests.
2. Run them and show that they fail because the production module is absent.
3. Implement the smallest contract that passes them.
4. Run both commands above.
5. Inspect `git status --short` and confirm that you changed only the two
   allowed files.

In your final response, list the changed files, exact test counts and results,
assumptions, and remaining limitations. Do not claim that the contract is
integrated with inference, receipts, approval, or execution.

## Evidence reminder for the human operator

Capture three full-window screenshots with CodeBuddy visibly identified:

1. This original prompt and CodeBuddy's scope acknowledgement.
2. The test-first implementation with only the two allowed files visible.
3. Passing focused tests and CodeBuddy's final changed-files summary.

Preserve the original CodeBuddy session JSONL, the two-file diff, independent
test output, and a completed copy of
`docs/codebuddy/evidence-log-template.md`.
