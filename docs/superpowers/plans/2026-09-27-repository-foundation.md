# Repository Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the current working directory into a clean, reproducible Python 3.12 repository that teammates can clone and run without receiving secrets, runtime data, personal paths, model weights, or generated artifacts.

**Architecture:** Keep the existing `apps/dimon_live` application intact while adding one root `uv` project, explicit launch commands, repository-boundary checks, and project-level documentation. Initialize a fresh Git history only after the tracked-file boundary passes automated inspection.

**Tech Stack:** Python 3.12, uv, FastAPI, Uvicorn, unittest, Git.

**Spec:** [Team repository and inference architecture design](../specs/2026-09-27-team-repository-and-inference-architecture-design.md)

## Global Constraints

- Use `uv` as the teammate setup path. Docker remains deferred.
- Never track `.env`, SQLite databases, visitor ledgers, generated audio, caches, model weights, compiled inference runtimes, tunnel credentials, or personal absolute paths.
- Preserve `llama.stocksuite.app` and `jarvis.stocksuite.app`; this plan does not edit or deploy either route.
- Target Python `>=3.12,<3.13` and commit `uv.lock`.
- Preserve the existing application behavior and 197-test baseline.
- Do not add Codex or ChatGPT contributor metadata, commit trailers, or acknowledgements. Credit CodeBuddy only for work actually performed through CodeBuddy.

## Review Focus

- A fresh clone must install and run without reading files outside the clone.
- `git ls-files` must contain no secret, database, model, runtime, cache, or generated-audio files.
- Example configuration must contain names and safe defaults only, with no usable credentials.
- The root launcher must bind to loopback by default.
- Existing tests must remain green after packaging changes.

---

### Task 1: Define and test the publication boundary

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_repository_boundary.py`
- Create: `scripts/check-repository-boundary`
- Modify: `.gitignore`
- Modify: `experiments/local-structured-action/README.md`

**Interfaces:**
- `scripts/check-repository-boundary` exits nonzero when tracked candidates contain forbidden paths, file suffixes, credential-shaped values, or `/home/<user>/...` paths.
- `tests/test_repository_boundary.py` imports the same rule set and covers both allowed examples and rejected artifacts.

- [ ] Write failing tests covering `.env`, `*.sqlite3`, `runtime/`, model formats (`.gguf`, `.safetensors`, `.bin`, `.onnx`), generated audio, caches, Cloudflare credential files, the competition PDF, and personal absolute paths.
- [ ] Run `python3 -m unittest tests.test_repository_boundary -v`; confirm the new boundary module or script is missing.
- [ ] Implement the checker with an explicit allow-list for `.env.example`, fixture metadata, and documentation placeholders.
- [ ] Expand `.gitignore` for active environments, runtime state, model/runtime artifacts, editor files, test caches, coverage output, and generated audio.
- [ ] Replace the personal Qwen path in the experiment README with `${QWEN_MODEL_PATH}`.
- [ ] Run `python3 -m unittest tests.test_repository_boundary -v` and `scripts/check-repository-boundary`; confirm both pass.
- [ ] Commit the boundary as `chore: define repository publication boundary`.

### Task 2: Add the root uv project and application entry point

**Files:**
- Create: `pyproject.toml`
- Create: `.python-version`
- Create: `apps/dimon_live/cli.py`
- Create: `tests/test_cli.py`
- Generate: `uv.lock`

**Interfaces:**
- Console command: `dimon-serve`.
- `apps.dimon_live.cli:main()` reads `DIMON_HOST` and `DIMON_PORT`, defaulting to `127.0.0.1:8890`, then starts `apps.dimon_live.app:app` with Uvicorn.
- `pyproject.toml` defines one installable package and groups test-only dependencies under `[dependency-groups].dev`.

- [ ] Write failing tests that assert the CLI defaults to loopback, accepts an explicit port, and never starts model processes.
- [ ] Run `python3 -m unittest tests.test_cli -v`; confirm `apps.dimon_live.cli` is missing.
- [ ] Add project metadata, the Python constraint, runtime dependencies compatible with the validated environment (`fastapi`, `httpx`, `pydantic`, `python-dotenv`, `uvicorn[standard]`, and `websockets`), and the `dimon-serve` script.
- [ ] Implement the small CLI wrapper with injectable `uvicorn.run` for tests.
- [ ] Run `uv lock`, then `uv sync --locked --group dev`.
- [ ] Run `uv run python -m unittest tests.test_cli -v` and the existing application test suite.
- [ ] Commit the packaging work as `build: add reproducible uv project`.

### Task 3: Create teammate-facing repository documentation

**Files:**
- Create: `README.md`
- Create: `LICENSE`
- Create: `THIRD_PARTY_NOTICES.md`
- Create: `.env.example`
- Modify: `apps/dimon_live/.env.example`
- Modify: `docs/README.md`
- Test: `tests/test_repository_docs.py`

**Interfaces:**
- Root quick start uses `uv sync --locked --group dev` and `uv run dimon-serve`.
- `.env.example` documents fixture, remote, and local mode names without embedding secrets or personal filesystem paths.
- `THIRD_PARTY_NOTICES.md` lists Qwen, Nemotron/NeMo, Kokoro, llama.cpp, and project dependencies with upstream names and license-verification links; it does not claim that model weights are redistributed.

- [ ] Write failing documentation-contract tests for the quick start, Python version, three inference modes, model-weight exclusion, fixture default, and security boundary.
- [ ] Run `python3 -m unittest tests.test_repository_docs -v`; confirm the root files are absent.
- [ ] Write the root README with architecture, setup, test commands, repository layout, contributor workflow, and a clear statement that weights live outside Git.
- [ ] Add the MIT license for repository-owned code and a third-party notice file that keeps upstream licenses separate.
- [ ] Consolidate safe application settings into the root example and keep the app-local example as a compatibility pointer during migration.
- [ ] Link this implementation series from `docs/README.md`.
- [ ] Run the documentation-contract and boundary tests.
- [ ] Commit the teammate documentation as `docs: add repository setup guide`.

### Task 4: Initialize the fresh Git history after the audit

**Files:**
- Recreate: `.git/` through `git init -b main`

**Interfaces:**
- The first commit contains only files accepted by `scripts/check-repository-boundary`.
- Commit authorship comes from the developer's configured Git identity; no generated co-author trailers are added.

- [ ] Confirm the existing `.git` directory is not a valid repository and contains no history that needs preservation.
- [ ] Remove only that empty/invalid metadata directory, then run `git init -b main`.
- [ ] Stage an explicit reviewed file list; do not use a blind `git add -A` until the boundary checker supports `git ls-files --cached`.
- [ ] Run the boundary checker against the staged index and inspect `git diff --cached --stat` plus `git diff --cached --check`.
- [ ] Commit with a plain project-owned message and inspect `git log -1 --format=fuller` for unwanted attribution.

### Task 5: Prove the clean-clone workflow

**Files:**
- Modify: `scripts/check-repository-boundary`
- Modify: `README.md`
- Test: `tests/test_clean_clone.py`

**Interfaces:**
- The clean-clone test creates a temporary local clone, verifies ignored runtime files are absent, and runs the offline-safe test subset from the clone.

- [ ] Write a failing test that clones from the local Git repository into a temporary directory and asserts forbidden files and absolute personal paths are absent.
- [ ] Extend the checker to inspect the committed tree with `git ls-files` when Git is available, while retaining a pre-initialization filesystem mode.
- [ ] From a temporary clone, run `uv sync --locked --group dev`, `uv run python -m unittest discover -s apps/dimon_live/tests -p 'test_*.py'`, and `uv run python -m compileall -q apps services tests` (omit `services` until it exists).
- [ ] Record the exact clean-clone commands in the README.
- [ ] Commit the verified clean-clone contract.

### Verification Contract

Before reporting this checkpoint complete, run:

```bash
scripts/check-repository-boundary
uv lock --check
uv sync --locked --group dev
uv run python -m unittest discover -s apps/dimon_live/tests -p 'test_*.py'
uv run python -m unittest discover -s tests -p 'test_*.py'
uv run python -m compileall -q apps tests
git diff --check
git status --short
```

Create one temporary local clone and repeat the sync and test commands there. The checkpoint is complete when the clone succeeds without any file outside the repository and the committed tree contains no forbidden artifact.
