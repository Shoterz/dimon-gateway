# Dimon Gateway: Linux groundwork proposal

Date: 2026-09-18. Status: draft for discussion; implementation has not started.

Confirmed runtime choice: Qwen served through llama.cpp. First action: agree on a small demo journey and its must-have features before importing the frontend foundation. Clone the existing source for inspection if useful; select the reusable components and repository setup after confirming scope and reuse eligibility.

## Objective and first demo

Build a conversational mock banking demo that turns a payment request into a structured proposal, independently checks it, presents the exact details for explicit approval, and executes it once through a server-owned ledger.

Initial scope: English, SGD, seeded internal recipients, one payment at a time, browser push-to-talk, typed input fallback. Example: “Send Alice 25 dollars.” An ambiguous recipient or amount requires clarification. No real funds are involved.

Success means the approved recipient and amount match the executed transaction, the ledger changes exactly once, and attempts to bypass approval fail at the gateway. Speech recognition and model output are fallible; the review screen remains necessary.

## Observed Linux environment

Read-only checks in the current session found:

| Item | Observation |
| --- | --- |
| OS | Pop!_OS 22.04 LTS, x86-64 |
| CPU | Intel Core i5-13400F |
| Memory | 31 GiB reported, approximately 32 GB installed |
| Workspace disk | Approximately 136 GB available |
| JavaScript | Node 24.18.1, pnpm 10.12.1 |
| Python | Python 3.10.12, uv available |
| Other tools | Docker, Compose, FFmpeg available; llama.cpp installation not yet checked |
| GPU | NVIDIA PCI device visible; no NVIDIA device files accessible and nvidia-smi fails in this session |
| Project | No application files; git status cannot read a valid repository here |

Binary presence does not establish that Docker is running. GPU model, VRAM, host driver status, llama.cpp build, and inference latency remain unverified. Sandbox isolation may explain the inaccessible GPU.

## Recommended approach and alternatives

Recommended: one repository with a React/Vite frontend, a Python conversation service, and a TypeScript gateway. Start the mock payment flow with typed input, then add local inference and voice. This makes approval enforcement testable without waiting for GPU setup.

Alternative: use a permitted hosted inference provider behind the same conversation interface if local latency is unsuitable. This adds network dependence and requires checking the hackathon's model and deployment requirements.

Alternative: build a fresh minimal frontend if reuse is disallowed or extracting the existing UI takes longer than building the small demo screen. Preserve the gateway and conversation interfaces either way.

## Proposed structure and responsibilities

```text
dimon-gateway/
  apps/web/                  React/Vite interface
  services/conversation/     Python/FastAPI: transcription, Qwen drafts, speech
  services/gateway/          TypeScript/Fastify: policy, approval, execution
  packages/contracts/       Versioned JSON schemas and matching validators
  evals/                    Legitimate requests, attacks, evaluation runner
  docs/                     Design, provenance, results, setup instructions
```

Use pnpm for JavaScript packages and uv for an isolated Python environment. Proposed project Python version: 3.11, with dependencies locked after a compatibility smoke check. Keep the operating system's Python unchanged. Pin Node 24 and package-manager versions; commit lockfiles. Node 24 is currently an LTS release. [Node release information](https://nodejs.org/en/about/previous-releases).

The contract source should be language-neutral JSON Schema, since TypeScript types alone cannot validate Python or HTTP requests. Core records: PaymentProposal, AuditResult, ApprovalRecord, ExecutionReceipt. Amounts use integer minor units; identities come from authenticated server context and seeded recipient IDs.

SQLite is sufficient for this single-machine mock demo. Only the gateway owns the database and signing secret. Authentication may reuse Firebase login, but the gateway must verify identity independently before granting account access or approval.

For the isolated demo configuration, run services as separate non-root containers with the ledger volume and approval secret mounted only into the gateway. Browser, conversation service, and model runtime receive no ledger write access or approval signing secret. Separate directories or host processes running as the same user do not establish this isolation.

## Payment and approval flow

1. Browser submits typed input or records a short audio clip.
2. Conversation service transcribes audio and asks Qwen for a schema-constrained proposal. It cannot approve or execute payments.
3. Gateway validates the schema, resolves known recipients, verifies ownership, and runs deterministic policy checks. Missing or ambiguous fields return a clarification request.
4. Browser displays the gateway's stored proposal: source account, recipient, currency, amount, and any fee. Initial demo fee is zero.
5. An authenticated approval endpoint records explicit user confirmation of that proposal version and issues a server-signed, short-lived approval bound to its canonical transaction hash, user ID, proposal version, expiry, and unique nonce.
6. Gateway verifies approval and atomically consumes it while applying balanced ledger entries. Retries return the existing receipt; they never debit again. Editing payment details invalidates the previous approval.
7. Conversation service speaks a fixed summary derived from the gateway receipt. It must not announce success from a model-generated claim.

“Independent audit” initially means deterministic checks outside the conversational model: valid fields, positive amount, seeded recipient, account ownership, currency, limits, and available funds. Recheck funds and account state during atomic execution. These checks do not guarantee semantic agreement with speech or catch every malicious request; user review and measured evaluations address that remaining risk. An optional second model can add advisory checks later.

A spoken “yes” never reaches the approval path. A server signature proves the approval record came from the gateway; it does not prove a human understood the transaction. Session authentication and request protections remain necessary. Do not describe the mock click confirmation as bank-grade authorization.

## Local model baseline

- Qwen through llama.cpp's llama-server: select a supported Qwen GGUF model and benchmark a small quantized baseline; compare a larger model only after confirming usable GPU memory. Record the exact model checksum, quantization, llama.cpp build/commit, inference settings, and prompt version. [llama.cpp server documentation](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).
- Require JSON-schema output and validate it again at the gateway. Schema conformance does not establish that a recipient or amount is correct. [llama.cpp schema-constrained responses](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).
- Whisper transcription through faster-whisper: start with base.en or small.en, CPU int8 as an available fallback, and benchmark names and amounts on representative recordings. GPU execution has separate CUDA/cuDNN requirements. [faster-whisper documentation](https://github.com/SYSTRAN/faster-whisper).
- Kokoro-82M for speech output: begin with one English voice. The documented Python setup includes espeak-ng, which was not found on the current PATH. [Kokoro model card and setup](https://huggingface.co/hexgrad/Kokoro-82M).

Start with short audio clips and bounded inference context. Avoid downloading many model variants or running fine-tuning before measuring the baseline.

## Milestones and completion criteria

| Order | Work | Evidence required |
| --- | --- | --- |
| 1 | Confirm constraints and Linux runtime | Deadline/team known; brief reviewed; host GPU and microphone checked; model restriction and reuse ruling recorded |
| 2 | Establish contracts and mock gateway | Seeded ledger; manual proposal; authenticated review/approval; atomic execution; replay and tampering rejected |
| 3 | Complete typed conversational flow | Typed request produces a reviewed payment and receipt; ambiguous requests ask for clarification |
| 4 | Add voice | Browser recording → Whisper → same proposal path; Kokoro speaks actual receipts; typed fallback works |
| 5 | Demonstrate attacks and evaluate | Missing approval, changed amount/recipient, replay, expired approval, cross-user access, concurrent execution, and insufficient funds tested |
| 6 | Package the demo | Reproducible startup, ledger reset, model manifest, architecture/provenance notes, evaluation results, recording and submission assets |
| Optional | Fine-tuning comparison | Working baseline preserved; unseen attacks and legitimate requests evaluated using the same protocol |

Keep gateway correctness checks separate from model evaluations. Report recipient/amount/currency accuracy, clarification rate, legitimate-request refusals, draft manipulation, unauthorized execution, duplicate execution, and latency per stage. Distinguish attacked drafts from actual ledger changes. Zero unauthorized executions in a finite test suite is evidence for that suite, not a universal guarantee.

Use a small development set first, then reserve an unseen evaluation set. Store case IDs, inputs, proposals, audit results, ledger outcomes, and model configuration so claims can be reproduced. Set numeric latency and accuracy targets after the first benchmark and deadline are known.

## Reuse and submission constraints

The existing [DeFi-Wealth-Hub repository](https://github.com/zanceballos/DeFi-Wealth-Hub) describes statement/Gmail ingestion, a Firestore dashboard, and AI financial advice. Treat it as a potential UI foundation rather than the payment execution system.

Before importing code, confirm organizer acceptance of prior hackathon work and permission to reuse the relevant source/assets. Record the source commit and retained components, preserve applicable attribution, and list the newly built payment features. Until then, continue planning the original gateway, contracts, and evaluation work. If reuse is not allowed, implement the minimal UI from scratch.

The competition brief is now present and reviewed at
[`docs/competition handbook/tencent hackathon brief.pdf`](competition%20handbook/tencent%20hackathon%20brief.pdf).
It confirms the DBS Direct Conversational Transaction Agent challenge and
specifically calls for multi-intent parsing, dynamic ambiguity resolution,
independent read-only discrepancy auditing, deterministic human-in-the-loop
authorization, zero-trust gateway policies, injection shielding, risk/velocity
controls, and a traceable audit trail. Team eligibility, registration,
track-specific preliminary criteria, deployment constraints, and submission
portal details still require confirmation.

## Team workflow for multiple services

Treat repository layout, runtime location, and task ownership as separate decisions. One repository does not require one development machine, and a model being local to the owner does not prevent teammates calling it remotely through a controlled API.

Recommended initial arrangement:

```text
Each teammate's laptop
  web frontend + gateway + own seeded development ledger
                 |
                 | authenticated AI requests over a private network
                 v
Owner's Linux PC
  conversation API: draft / transcribe / speak
       |              |             |
   llama.cpp        Whisper        Kokoro
      Qwen

Integrated demo
  one reviewed commit, one web frontend, one gateway/ledger,
  and the real conversation API
```

The shared conversation API supplies proposals and audio only. Each local gateway independently validates proposals and controls its own approvals and ledger. Teammates do not share writable ledger files or the owner's filesystem. Keep three application boundaries (web, conversation, gateway); Whisper, Qwen, and Kokoro initially remain adapters behind the conversation service rather than three additional independently deployed applications. llama-server remains a separate model runtime on the GPU host.

Define the HTTP interfaces before splitting implementation tasks: transcription accepts audio and returns text; drafting accepts a request and returns either a validated proposal or a clarification; speech accepts a bounded text summary and returns audio. Agree on JSON examples, error shapes, IDs, audio formats, and limits. Keep approval/execution interfaces exclusively on the gateway.

Provide three development configurations:

| Configuration | Runs locally | AI dependency | Purpose |
| --- | --- | --- | --- |
| Mock | Web, gateway, seeded ledger, fixture responses | None | UI and approval development even while inference is offline |
| Hybrid | Web, gateway, seeded ledger | Shared real conversation API on Linux PC | Individual integration work without a GPU on each laptop |
| Full demo | Integrated web, gateway, ledger, conversation and models | Real model runtime | Evaluation and the final demonstration |

Mock responses must be visibly identified; results from this mode cannot be reported as model performance. Start with fixtures for a valid payment, ambiguous recipient, policy rejection, inference timeout, and successful receipt. No configuration may replace the gateway approval checks with a mock bypass.

Use Docker Compose to describe the backend's environment, networking, volumes, and health checks. Frontend developers can run Vite directly for fast edits. Proposed convenience commands are make dev, make dev-mock, make demo, and make reset-demo; these have not been implemented. Compose supports optional profiles and waiting for dependency health, but services still need request timeouts and runtime error handling. [Compose profiles](https://docs.docker.com/compose/how-tos/profiles/), [startup readiness](https://docs.docker.com/compose/how-tos/startup-order/).

For a four-person team, proposed ownership is frontend/review experience, conversation/voice/model baseline, gateway/ledger/security, and evaluation/demo/integration. With fewer people, combine roles. Each developer has their own branch and checkout; integrate small changes into main regularly. Assign one person to maintain a working integrated demo, and run the complete payment path early and repeatedly rather than first combining everything near the deadline.

Central inference initially needs a bounded request queue and conservative concurrency, with exact limits selected by benchmarking. CPU voice processing may leave more GPU capacity for Qwen; test this rather than assuming it is faster. Keep the selected model warm during demo sessions, bound context/audio sizes, record per-stage latency, and expose useful health/readiness status without leaking secrets. Use correlation IDs across drafting, audit, approval, and receipt logs. Do not introduce Kubernetes or a distributed message broker unless measured needs justify them.

The owner deploys reviewed AI-service changes to the shared host; other teammates do not need host SSH for normal work. Share access credentials outside Git. Configure authentication and network restrictions on the shared conversation API; keep llama-server reachable only by the conversation service. [llama.cpp server options](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).

Keep the Linux PC awake and available during agreed integration windows. Preserve the mock development configuration for outages. Before judging, check venue connectivity, microphone access, cold/warm model latency, and a clean ledger reset. Prefer a localhost browser on the demo machine for the first voice demo; remote browser microphone capture requires a suitable secure origin. Record a backup demonstration, and disclose any prerecorded or mocked elements when showing them.

This is a recommended workflow for Dimon Gateway, not a claim that all OpenAI, Anthropic, or Stanford hackathon teams use it. Official examples establish that API credits and starter projects remain relevant: [OpenAI hackathon support](https://openai.com/form/hackathon-support/) and [TreeHacks resources](https://live.treehacks.com/resources).

## Future team access and SSH

Recommendation: teammates develop in their own repository clones first. Share only the model inference capability they need through an authenticated, restricted endpoint over a private network. This avoids giving everyone a shell on the personal workstation. Do not expose the raw model server to the public internet.

If remote development on this PC is necessary, prefer a dedicated Linux development VM. Give each teammate an individual account and SSH key inside the VM. Keep personal directories, SSH agents, host Docker sockets, and host credentials out of it. Use Git to exchange changes rather than mounting the personal home directory. Limit guest CPU, RAM, and disk; restrict guest access to the host and LAN; keep snapshots and separate Git backups. A VM is a stronger isolation choice for this plan, not an absolute security guarantee. Initially retain inference on the host behind the restricted API rather than assuming GPU passthrough is required or straightforward.

| Access model | Suitable use | Main limitation |
| --- | --- | --- |
| Own clones plus shared inference | Default team workflow | Requires a secured inference interface and access controls |
| Dedicated development VM | Teammates need remote shells on this PC | More setup; guest network and host integrations need deliberate restrictions |
| Separate host accounts | Small group of trusted teammates; accepted residual risk | Same host kernel, permitted filesystem access, reachable services, and shared resources |
| SFTP-only chroot | Upload/download files | Does not support normal terminal development |

A normal SSH account cannot be made “this folder only” by choosing a home directory, starting the shell there, or using a restricted shell. Unix permissions restrict access to individual paths; users can still read permitted system files, execute code, contact reachable services, and consume shared resources. A writable checkout also permits deleting/copying its contents and changing its scripts. [Ubuntu user management](https://ubuntu.com/server/docs/how-to/security/user-management/).

OpenSSH chroot supports a restricted filesystem view, but its root and parent directories must be root-owned and not writable by teammates. A full development shell needs an entire runtime inside it. SFTP-only chroot is much simpler; neither option should be presented as equivalent to a dedicated VM. SSH forwarding restrictions also do not stop a shell user from creating their own network connections. [OpenSSH configuration reference](https://man.openbsd.org/sshd_config).

If host accounts are chosen, require all of the following before access:

- One account and public key per person; never share the personal ylz login or private keys. Restrict SSH to a private VPN network with explicit membership/access rules. Disable root SSH login and password/keyboard-interactive login after verifying key access.
- No sudo, rootful docker group membership, Docker socket access, privileged containers, or unrestricted host-management privileges. The docker group grants root-level privileges. [Docker post-installation documentation](https://docs.docker.com/engine/install/linux-postinstall/).
- Put future team checkouts outside the personal home, for example under /srv/dimon-gateway, using a dedicated project group or targeted ACLs. Do not add teammates to the personal ylz group or loosen the personal home to make this Desktop path accessible. The observed personal home mode is 750; other ACLs and sensitive-path permissions have not been audited.
- Give each teammate a separate checkout/branch and development database. Keep production/cloud credentials and deployed approval signing secrets outside writable development trees. Any credential readable by an account should be considered available to code running as that account.
- Authenticate local services too: binding a service to localhost does not hide it from another host account. Apply resource limits and disk quotas; retain Git backups and test restoration.
- Before SSH configuration reload, validate syntax and effective per-user settings, keep the owner's existing session open, and test a second login. Test that the teammate account cannot read personal keys, obtain sudo, use the host Docker daemon, or access protected service credentials. [Ubuntu OpenSSH setup and validation](https://ubuntu.com/server/docs/how-to/security/openssh-server/).
- Offboarding removes VPN permissions and authorized keys, disables login, terminates existing sessions/background processes, and rotates exposed shared secrets. Disabling a login alone does not stop existing processes; copies already made cannot be recalled.

The separate service permissions in the application protect against the conversational model's normal capabilities. They cannot protect against a developer who controls gateway code and runs it with ledger credentials. Restrict deployed gateway administration to the owner; use disposable development secrets and mock balances for team work. Review teammate changes before running them under the owner's account.

No host accounts, SSH configuration, firewall rules, or filesystem permissions were changed during planning.

## Immediate next decisions

- [x] Receive and review the DBS banking brief.
- [ ] Confirm the submission deadline, team eligibility, registration, and any track-specific preliminary rubric.
- [ ] Verify host GPU and microphone access, then benchmark the selected local model and voice path.
- [x] Confirm English, SGD, seeded synthetic accounts, and a local-only mock ledger as the first-demo scope.
- [ ] Review the current architecture against the brief before adding new external integrations.
