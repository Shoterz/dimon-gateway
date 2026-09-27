# CodeBuddy work-packet template

Copy this template into a new Markdown file for each CodeBuddy contribution.
Fill every bracketed field before starting the session.

## Work packet

**Outcome:** [one observable behavior]

**DBS DCTA requirement:** [one requirement from the brief]

**Current behavior:** [what the repository does now]

**Allowed files:**

- `[path]`
- `[path]`

**Forbidden changes:**

- Do not edit files outside the allowed list.
- Do not install packages or change runtime configuration.
- Do not weaken or delete existing tests.
- Do not add ledger, receipt, approval, credential, shell, or SQL authority.

**Acceptance checks:**

- `[test command]`
- `[focused behavior and expected result]`

**Stop conditions:** Stop and ask before continuing if the task requires another
file, a database migration, a dependency, an interface change outside the named
contract, or a weaker safety rule.

## Paste-ready CodeBuddy prompt

```text
You are contributing one bounded feature to the Dimon synthetic DBS DCTA demo.

Read these files first:
- [path]
- [path]

Implement this outcome:
[one observable behavior]

The work supports this requirement from the DBS brief:
[brief requirement]

You may change only:
- [path]
- [path]

Constraints:
- Keep the project synthetic and local-only.
- Do not connect to a bank, PayNow, brokerage, or production identity system.
- Do not add ledger-write, approval, receipt-signing, shell, SQL, or credential authority.
- Do not install dependencies, refactor unrelated code, or weaken existing tests.
- Treat model output as untrusted and fail closed on malformed or unknown values.

Before editing, state your short plan and confirm that it fits the allowed files.
Then write focused tests, implement the smallest change that passes them, and run:
[test command]

In your final response, list changed files, test output, remaining limitations,
and any assumption you made. Stop and ask if the work needs broader scope.
```

## Evidence to preserve

- The complete CodeBuddy conversation or exported history.
- Screenshots that show the original prompt, implementation work, and test
  result. The full submission still needs at least three CodeBuddy or WorkBuddy
  screenshots.
- The resulting diff and fresh local test output.
- The completed review record based on
  [`evidence-log-template.md`](evidence-log-template.md).
