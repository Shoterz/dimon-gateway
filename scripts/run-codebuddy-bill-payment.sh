#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
DEFAULT_PROJECT="$(cd -- "$SCRIPT_DIRECTORY/.." && pwd -P)"
PROJECT="${DIMON_CODEBUDDY_PROJECT:-$DEFAULT_PROJECT}"
PACKET="${DIMON_CODEBUDDY_PACKET:-$PROJECT/docs/codebuddy/bill-payment-contract.md}"
DEFAULT_WRAPPER="${HOME:?HOME must be set}/Desktop/corebuddy/codebuddy"
WRAPPER="${DIMON_CODEBUDDY_WRAPPER:-$DEFAULT_WRAPPER}"
EXPECTED_BRANCH="lz-stock-feature"

usage() {
  printf '%s\n' \
    'Usage: scripts/run-codebuddy-bill-payment.sh [--check|--print-prompt|--help]' \
    '' \
    'With no option, verify the evidence baseline and open an interactive' \
    'CodeBuddy session with the bounded BILL_PAYMENT work packet.' \
    '' \
    '  --check         Verify that the branch, worktree, wrapper, and packet are ready.' \
    '  --print-prompt  Print the exact prompt without starting CodeBuddy.' \
    '  --help          Show this help.'
}

fail() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

case "${1:-}" in
  --help|-h)
    usage
    exit 0
    ;;
  --print-prompt)
    [[ -f "$PACKET" ]] || fail "work packet not found: $PACKET"
    cat "$PACKET"
    exit 0
    ;;
  --check|'')
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

[[ -d "$PROJECT/.git" ]] || fail "project is not a Git repository: $PROJECT"
[[ -x "$WRAPPER" ]] || fail "CodeBuddy wrapper is not executable: $WRAPPER"
[[ -f "$PACKET" ]] || fail "work packet not found: $PACKET"

current_branch="$(git -C "$PROJECT" branch --show-current)"
[[ "$current_branch" == "$EXPECTED_BRANCH" ]] || \
  fail "must run on branch $EXPECTED_BRANCH; current branch is ${current_branch:-detached}"

if [[ -n "$(git -C "$PROJECT" status --porcelain --untracked-files=no)" ]]; then
  fail "tracked working tree must be clean before CodeBuddy starts; commit or stash existing tracked work"
fi

for contribution_file in \
  "$PROJECT/apps/dimon_live/bill_payment_contract.py" \
  "$PROJECT/apps/dimon_live/tests/test_bill_payment_contract.py"
do
  [[ ! -e "$contribution_file" ]] || \
    fail "bounded contribution file already exists: $contribution_file"
done

if [[ "${1:-}" == "--check" ]]; then
  printf 'READY: %s has a clean tracked baseline on %s; target files, work packet, and CodeBuddy wrapper are ready.\n' \
    "$PROJECT" "$EXPECTED_BRANCH"
  exit 0
fi

printf '%s\n' \
  'Starting the bounded CodeBuddy BILL_PAYMENT contribution.' \
  'Capture three full-window screenshots:' \
  '  1. Original prompt and CodeBuddy scope acknowledgement.' \
  '  2. Test-first implementation with the two allowed files visible.' \
  '  3. Passing focused tests and CodeBuddy final summary.' \
  'Do not approve edits outside the two files named in the prompt.' \
  ''

prompt="$(cat "$PACKET")"
export CODEBUDDY_PROJECT="$PROJECT"
exec "$WRAPPER" "$prompt"
