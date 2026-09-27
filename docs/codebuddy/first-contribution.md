# First CodeBuddy contribution: strict validation-result contract

**Status:** prepared for a real CodeBuddy session; no CodeBuddy work or evidence
has occurred yet.

## Outcome

CodeBuddy adds a strict, read-only contract for an independent validation
engine. The contract accepts per-field `MATCH`, `MISMATCH`, or `UNKNOWN`
decisions and blocks an overall pass unless every required field matches.

This contribution supports the DBS brief's Dual-Engine Semantic Discrepancy
Auditing requirement. It gives CodeBuddy a visible part of the transaction
safety design while keeping it away from model orchestration, receipts,
approval, execution, and SQLite.

## Allowed files

- `apps/dimon_live/validation_contract.py`
- `apps/dimon_live/tests/test_validation_contract.py`

CodeBuddy may create both files. It may inspect nearby source and tests but may
not edit them.

## Required behavior

Implement a dependency-free Python contract with:

- Required critical fields: `action_kind`, `source_account`, `recipient`,
  `amount`, and `currency`.
- Allowed verdicts: `MATCH`, `MISMATCH`, and `UNKNOWN`.
- A parser that rejects missing fields, duplicate fields, extra fields, invalid
  verdicts, empty reason codes, and non-string evidence.
- An overall `PASS` result only when each required field has a `MATCH` verdict.
- An overall `BLOCK` result for either `MISMATCH` or `UNKNOWN`.
- No accepted key or output that represents approval, execution, a receipt
  digest, canonical account IDs, SQL, or a tool call.

Use immutable data objects and the Python standard library. Treat input as
untrusted model output.

## Focused tests

The tests must cover:

- Five matching fields produce `PASS`.
- One mismatch produces `BLOCK`.
- One unknown produces `BLOCK`.
- Missing, duplicate, extra, and invalid fields raise the contract error.
- Approval or execution-shaped keys fail validation.
- Parsing does not import or call database, receipt, or service modules.

Run:

```bash
python3 -B -m unittest apps.dimon_live.tests.test_validation_contract -v
python3 -B -m unittest discover -s apps/dimon_live/tests -q
```

## Paste-ready CodeBuddy prompt

```text
You are contributing one bounded feature to the Dimon synthetic DBS DCTA demo.

Read these files before editing:
- apps/dimon_live/action_intent.schema.json
- apps/dimon_live/tests/test_model_adapter.py
- docs/competition handbook/dbs-banking-playbook.md

Add a strict, read-only validation-result contract. An independent validation
engine will use this contract later to compare a transaction draft with its
transcript. This task does not connect that engine or change execution.

You may create or change only:
- apps/dimon_live/validation_contract.py
- apps/dimon_live/tests/test_validation_contract.py

Required behavior:
- Require action_kind, source_account, recipient, amount, and currency exactly once.
- Accept only MATCH, MISMATCH, or UNKNOWN for each field.
- Return overall PASS only when all required fields match.
- Return overall BLOCK when any field is MISMATCH or UNKNOWN.
- Reject missing, duplicate, extra, malformed, approval-shaped, and execution-shaped data.
- Require non-empty reason codes and string evidence.
- Use immutable data objects and the Python standard library.
- Treat all input as untrusted model output.

Constraints:
- Do not edit existing files.
- Do not install dependencies.
- Do not import or call the database, receipt, service, model, approval, or execution layers.
- Do not add SQL, shell access, tools, credentials, canonical IDs, receipt digests, or ledger authority.
- Do not weaken existing tests.

Before editing, state your short plan and confirm that it fits the two allowed
files. Write focused tests, implement the smallest contract that passes them,
and run:

python3 -B -m unittest apps.dimon_live.tests.test_validation_contract -v
python3 -B -m unittest discover -s apps/dimon_live/tests -q

In your final response, list changed files, exact test results, assumptions, and
remaining limitations. Stop and ask if the task requires broader scope.
```

## Evidence checklist

- Save the complete CodeBuddy conversation.
- Capture the opening prompt, implementation, and test result.
- Keep the resulting two-file diff.
- Run the focused and full test commands outside CodeBuddy.
- Complete a copy of [`evidence-log-template.md`](evidence-log-template.md).
