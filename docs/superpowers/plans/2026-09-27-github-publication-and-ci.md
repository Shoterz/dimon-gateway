# GitHub Publication and CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish a reviewable private GitHub repository with reproducible CI, safe collaboration rules, genuine contributor attribution, and a controlled path to public hackathon submission.

**Architecture:** Use the fresh local history created by the foundation plan, add GitHub automation that runs fixture-mode tests without GPUs or secrets, then push to a private `dimon-gateway` repository. Team collaboration happens through protected pull requests. Public visibility is a separate final action after a complete tracked-file, history, license, and secret audit.

**Tech Stack:** Git, GitHub CLI, GitHub Actions, uv, Python 3.12, unittest.

**Spec:** [Team repository and inference architecture design](../specs/2026-09-27-team-repository-and-inference-architecture-design.md)

**Platform reference:** [GitHub secure use guidance](https://docs.github.com/en/actions/reference/security/secure-use) for immutable action SHAs and least-privilege workflow credentials.

## Global Constraints

- Tasks 1 through 4 may begin after the fixture checkpoint. Protected-team rehearsal and final release evidence depend on the gateway, GPU host, and Cloudflare checkpoints.
- CI uses fixture mode and must not need a GPU, model files, Cloudflare secrets, or live host services.
- Create the remote as private first.
- Never rewrite or fabricate contributor identity. Do not add Codex/ChatGPT co-author trailers or acknowledgements. CodeBuddy appears as a contributor only through genuine authored commits made with the required CodeBuddy workflow.
- Do not commit GitHub, Cloudflare, model-host, or application secrets.
- Do not make the repository public until the user reviews the concrete private remote and explicitly approves the visibility change.

## Review Focus

- A pull request from a clean checkout must pass entirely in fixture mode.
- Actions must be pinned to immutable commit SHAs and receive minimum permissions.
- The full Git history, not only the current tree, must pass the publication audit.
- Branch protection must require the same checks that run in CI.
- README claims and demo instructions must match the deployed architecture and verified evidence.

---

### Task 1: Add contributor, security, and pull-request policy

**Files:**
- Create: `CONTRIBUTING.md`
- Create: `SECURITY.md`
- Create: `.github/pull_request_template.md`
- Create: `.github/CODEOWNERS`
- Modify: `README.md`
- Modify: `docs/codebuddy/README.md`
- Test: `tests/test_contributor_policy.py`

**Interfaces:**
- Contribution instructions require `uv sync --locked --group dev`, fixture-mode tests, boundary checks, no model artifacts, and no secrets.
- Pull requests declare behavior, validation, fixture impact, and deployment impact.
- Security reporting names a private contact mechanism rather than encouraging public secret disclosure.
- CodeBuddy instructions explain how to create a real bounded contribution and retain its authentic author metadata.

- [ ] Write failing tests for required contribution commands, prohibited attribution language, PR checklist fields, security contact placeholder resolution, and CODEOWNERS paths.
- [ ] Run `uv run python -m unittest tests.test_contributor_policy -v`; confirm the policy files are absent.
- [ ] Add concise collaboration and security documents using the actual repository commands and architecture.
- [ ] Update the existing CodeBuddy workflow so attribution is evidence-based and never added as a cosmetic trailer.
- [ ] Run the focused tests and repository-boundary checker.
- [ ] Commit the policies as `docs: add repository collaboration policy`.

### Task 2: Add GPU-free continuous integration

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `.github/dependabot.yml`
- Create: `scripts/ci`
- Create: `tests/test_ci_contract.py`

**Interfaces:**
- `scripts/ci` runs lock verification, repository/history boundary checks, Python compile checks, all app/gateway/host/template tests, and fixture smoke tests.
- GitHub Actions uses Python 3.12, `DIMON_INFERENCE_MODE=fixture`, read-only repository contents, no persisted credentials, dependency caching keyed by `uv.lock`, and a job timeout.

- [ ] Write failing tests for pull-request triggers, Python version, fixture mode, least-privilege permissions, immutable action SHAs, timeout, locked sync, and invocation of `scripts/ci`.
- [ ] Run the focused test and observe failures.
- [ ] Resolve the maintained official checkout, Python setup, and uv actions to their full commit SHAs; record the corresponding release tags in comments for review.
- [ ] Implement `scripts/ci` as the one local/remote validation entry point and add the minimal workflow around it.
- [ ] Configure Dependabot for GitHub Actions and Python dependencies with weekly grouped updates.
- [ ] Run `scripts/ci` locally and inspect the workflow with the contract test.
- [ ] Commit automation as `ci: add GPU-free verification`.

### Task 3: Audit the complete Git tree and history

**Files:**
- Modify: `scripts/check-repository-boundary`
- Create: `scripts/audit-git-history`
- Create: `tests/test_history_audit.py`
- Create: `docs/evidence/publication-audit.md`

**Interfaces:**
- `scripts/audit-git-history` scans every reachable blob name and bounded text blob for forbidden artifacts, credential patterns, personal paths, oversized files, and prohibited attribution.
- The audit fails on blobs over the repository size threshold and identifies only object/path metadata, never printing secret values.

- [ ] Write failing tests using a temporary Git repository containing a deleted secret, deleted model-shaped file, oversized blob, personal path, and prohibited co-author trailer.
- [ ] Run the focused test and observe failures.
- [ ] Implement history enumeration with `git rev-list --objects --all`, bounded blob reads, safe redaction, and a documented false-positive allow-list.
- [ ] Run current-tree and full-history checks, inspect all large files, and remove sensitive history before any remote push if the audit fails.
- [ ] Record sanitized audit commands, commit ID, file counts, largest allowed files, licenses, and pass/fail results.
- [ ] Commit the audit tooling as `chore: audit repository history`.

### Task 4: Create and validate the private GitHub repository

**Files:**
- Modify locally: `.git/config`
- Modify: `README.md`
- Modify: `docs/evidence/publication-audit.md`

**Interfaces:**
- GitHub repository name: `dimon-gateway` under the authenticated team owner's account or organization.
- Initial visibility: private.
- Default branch: `main`.

- [ ] Confirm GitHub CLI authentication and the intended owner without printing authentication tokens.
- [ ] Run the complete CI and history audit against the exact commit to publish.
- [ ] Create `dimon-gateway` as a private repository, add `origin`, and push `main`.
- [ ] Inspect the GitHub file list, language breakdown, Actions permissions, default branch, commit author list, and rendered README.
- [ ] Confirm active `.env`, databases, runtime files, audio, models, credentials, personal paths, competition PDFs, and invalid attribution are absent from both GitHub and history.
- [ ] Link the private repository URL and pushed commit into the sanitized publication evidence.
- [ ] Commit and push the sanitized evidence as `docs: record private publication audit`.

### Task 5: Configure protected team collaboration

**Files:**
- Modify: `CONTRIBUTING.md`
- Modify: `docs/development.md`

**Interfaces:**
- `main` requires pull requests, the CI check, resolved conversations, and no force pushes or deletions.
- Teammates receive least-privilege write access; CI receives read-only contents access.
- Remote integration secrets are stored only in teammate environments or protected GitHub environments, never ordinary pull-request secrets.

- [ ] Invite the named teammates with write access and confirm they can clone the private repository.
- [ ] Apply the branch protection rules and verify direct pushes are rejected using a disposable branch workflow.
- [ ] Have one teammate complete the fixture quick start and report the exact commands and any setup failure.
- [ ] Create one genuine CodeBuddy-authored bounded change through the documented workflow, open a pull request, and preserve its real author metadata.
- [ ] Confirm the pull request runs CI without model or Cloudflare secrets and merge it through the normal review path.

### Task 6: Rehearse judge setup and prepare the public-visibility decision

**Files:**
- Create: `docs/judge-runbook.md`
- Create: `docs/evidence/judge-rehearsal.md`
- Modify: `README.md`

**Interfaces:**
- Judge path A: visit `https://dimon.stocksuite.app` for the integrated demo.
- Judge path B: clone the repository and run deterministic fixture mode without a GPU.
- Optional host reproduction is documented separately and never presented as a requirement for basic review.

- [ ] Write the judge runbook with a five-minute fixture walkthrough, deployed-demo flow, expected outputs, architecture boundary, limitations, licenses, and troubleshooting.
- [ ] Rehearse from a clean machine or clean temporary environment using only the public-ready instructions.
- [ ] Verify every README and runbook link, hostname, command, screenshot, and claim against the exact release commit.
- [ ] Run CI, history audit, dependency/license review, Cloudflare verification, and the integrated demo one final time.
- [ ] Present the private repository URL, release commit, audit evidence, contributor list, known limitations, and planned visibility change to the user for explicit approval.
- [ ] After approval, change repository visibility to public, verify anonymous clone and Actions visibility, create a signed/tagged hackathon release, and record the public URL.

### Verification Contract

Before requesting public-release approval, run:

```bash
scripts/ci
scripts/check-repository-boundary
scripts/audit-git-history
scripts/verify-deployment
git diff --check
git status --short
gh repo view --json nameWithOwner,visibility,defaultBranchRef,url
gh run list --limit 5
```

The review package must include the private GitHub URL, exact commit SHA, passing CI run, clean history audit, verified contributor metadata, deployed-host checks, and judge rehearsal. Public visibility and release creation happen only after the user approves that concrete package.
