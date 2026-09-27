# Dimon Gateway

Dimon Gateway is a conversational-banking research demo for synthetic data. A
language model proposes a narrow intent, while deterministic Python owns
grounding, policy, receipts, approvals, session isolation, and SQLite ledger
mutation. Models never receive SQL, shell, Python execution, or approval
authority.

## Development setup

The repository targets **Python 3.12** and uses `uv` for dependency locking.

```bash
uv sync --locked --group dev
cp .env.example apps/dimon_live/.env
uv run dimon-serve
```

Open <http://127.0.0.1:8890>. The server binds to loopback by default.

The team architecture defines `fixture` as the default inference mode. The
fixture provider is the next implementation checkpoint; during this foundation
checkpoint, the existing application still uses its local adapters. Once the
provider checkpoint lands, the same command will run without models, CUDA, or
network access.

## Runtime modes

| Mode | Use | Inference source |
| --- | --- | --- |
| `fixture` | Daily teammate work and CI | Versioned deterministic repository fixtures |
| `remote` | Team integration testing | Authenticated `dimon-inference.stocksuite.app` gateway |
| `local` | GPU-host staging and demo | Loopback Qwen, Nemotron, and Kokoro services |

Mode selection changes inference wiring only. Deterministic policy, receipts,
approvals, and each developer's SQLite ledger remain local in every mode.

## Model boundary

The Git repository contains integration code, launch definitions, manifests,
fixtures, and tests. Model weights stay outside Git, alongside compiled
inference runtimes, and are stored once on the GPU host. Teammates do not need
to download
Qwen, Nemotron, Kokoro, CUDA, or llama.cpp for fixture-mode development.

The integrated host uses these private loopback services:

- Qwen through llama-server at `127.0.0.1:8080`;
- Nemotron streaming speech at `127.0.0.1:8004`; and
- Kokoro speech synthesis at `127.0.0.1:3001`.

Raw model ports remain private. Authenticated remote development will use the
bounded Dimon inference gateway, which exposes structured intent,
transcription, and committed-result speech rather than general model APIs.

## Repository layout

```text
apps/dimon_live/     FastAPI app, browser UI, deterministic policy, receipts, SQLite
services/            Project inference gateway and host adapters (planned checkpoints)
fixtures/inference/  Deterministic teammate and CI cases (planned checkpoint)
experiments/         Structured-action research and saved evaluation results
scripts/             Repository and host commands
tests/               Cross-component contracts
docs/                Architecture, development, runbooks, plans, and evidence
```

The [architecture design](docs/superpowers/specs/2026-09-27-team-repository-and-inference-architecture-design.md)
defines the complete boundary. The [implementation program](docs/superpowers/plans/2026-09-27-team-repository-program.md)
tracks the ordered checkpoints.

## Verification

```bash
uv lock --check
uv run python -m unittest discover -s apps/dimon_live/tests -p 'test_*.py'
uv run python -m unittest discover -s tests -p 'test_*.py'
scripts/check-repository-boundary
```

The boundary check rejects active environment files, databases, runtime state,
generated audio, model artifacts, credentials, personal paths, and the supplied
competition PDF.

### Clean-clone verification

Use a separate temporary directory to verify that setup does not read files
from the source checkout:

```bash
git clone --local --no-hardlinks /path/to/dimon-gateway /tmp/dimon-gateway-clean
cd /tmp/dimon-gateway-clean
uv sync --locked --group dev
uv run python -m unittest discover -s apps/dimon_live/tests -p 'test_*.py'
uv run python -m unittest discover -s tests -p 'test_*.py'
uv run python -m compileall -q apps tests
scripts/check-repository-boundary --committed
```

## Contributing

Use small reviewed commits and run the locked test suite before opening a pull
request. Never commit active `.env` files, model weights, generated ledgers,
tunnel credentials, or personal host paths. Preserve real Git authorship;
CodeBuddy attribution is retained only for work performed in a genuine
CodeBuddy session. See [the controlled contribution workflow](docs/codebuddy/README.md).

Repository-owned code is available under the [MIT License](LICENSE). External
models and runtimes retain their own terms; see [third-party notices](THIRD_PARTY_NOTICES.md).
