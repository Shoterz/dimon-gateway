# Dimon Live

## Public MVP gateway

Run the gateway from the repository root with:

```bash
./apps/dimon_live/run_public_mvp.sh
```

It binds only to `127.0.0.1:8890` and reuses the local Qwen, Nemotron, and
Kokoro services. Dimon acquires the existing Jarvis Nemotron lifecycle
supervisor on demand, waits until its private speech worker is ready, and
releases it when the voice turn ends. It does not create a second model stack. Copy
`.env.example` to `.env` and set `DIMON_ALLOWED_ORIGINS` to include the current
HTTPS ngrok origin. The stable owner hostname must be protected by Cloudflare
Access and `DIMON_OWNER_ACCESS_AUDIENCE` must match its Access JWT audience.

Every visitor receives an opaque `dimon_session` cookie and a separate SQLite
file under `runtime/ledgers/visitors/`. The owner ledger is persistent at
`runtime/ledgers/owner.sqlite3` and is never selected on the ngrok hostname.
There is one process-wide voice lease; additional voice users get `VOICE_BUSY`
and can use typed input. The voice button first shows **Starting speech model**,
then streams microphone audio while the user speaks and automatically submits
after roughly 1.4 seconds of silence. Clicking it again remains an immediate
manual-send fallback. Browser audio is resampled from its real device rate
(commonly 44.1 kHz on iPhone and 48 kHz on laptops) to the 16 kHz frames expected
by Nemotron. Safari's prefixed AudioContext is supported. Startup and transcript
deadlines always return the UI to a typed fallback instead of leaving it stuck.

Example tunnels:

```bash
ngrok http 8890
# Cloudflare Tunnel route: dimon.stocksuite.app -> http://127.0.0.1:8890
```

Dimon Live is a localhost-only, synthetic banking demo. Free-form text goes to
the existing local Qwen3.5 model for non-monetary intent extraction, while
deterministic Python derives the SGD amount and explicit PayNow mobile proxy
directly from the user's original text. Policy then either rejects the
proposal, asks for clarification, or atomically applies a balanced debit and
credit to SQLite.

Qwen never receives SQL, shell access, Python execution, or a general-purpose
tool. Every resolvable transfer becomes a server-created, SHA-256-bound
receipt. The browser can approve a stored receipt by ID and digest, but it
cannot name database account IDs or resubmit changed transfer facts.

Only a narrow affirmative command grammar is authorized by the synthetic
`EXPLICIT_COMMAND_POLICY`: an exact saved recipient plus one exact grounded
amount (or the amount followed by `to` and that recipient), optionally followed
by the exact resolved `from` account. Those commands settle immediately through
the receipt executor. Questions, memo/conditional tails, vague wording,
misspellings, and ambiguity display an exact stored receipt for confirmation.
Approval never invokes Qwen a second time. Missing
amounts use a stored continuation: the first turn locks source and recipient,
and the second turn is handled by a deterministic amount-only parser.

Qwen has no amount field in the version-three proposal schema. It cannot
convert dollars to cents or authorize a model-generated number. For example,
`$10` is parsed by Python as exactly `1000` minor units. No supported amount,
multiple amount expressions, a negative amount, or an unsupported form is
clarified or rejected before any receipt or ledger mutation.

Recipient resolution does not contain a dictionary of expected typos. It
normalizes and tokenizes the saved directory, then combines Jaro-Winkler
similarity with single-edit/transposition distance. Only an exact full saved
name is an exact match; first names, surnames, prefixes, and fuzzy matches are
candidate suggestions that require receipt confirmation. The resolver test
suite generates 198 spelling variations from the directory names and also
keeps unrelated-name and human-written holdout cases. A final Soundex-style
phonetic layer may suggest additional candidates, but it can never create an
automatic authorization.

The demo also contains one exact synthetic PayNow directory entry. Formatting
such as spaces, hyphens, and a `+65` prefix may normalize, but digits are never
corrected or fuzzy-matched. The returned masked name and destination account
come from the server-owned directory and are bound into the receipt. The UI
shows the directory-owned demo proxy, `8695 2933`, beneath the synthetic PayNow
account so it can be copied into a test command. When the original text
explicitly says PayNow and contains exactly one supported mobile proxy, Python
makes that destination authoritative even if Qwen classifies the request as a
saved-beneficiary transfer. Multiple explicit proxies are blocked rather than
guessed.

Saved-beneficiary batches use contract version four. Qwen proposes only two to
four recipient labels and verbatim recipient evidence; deterministic Python
extracts each SGD amount from the original text and pairs recipients and
amounts by source-text order. The gateway stores one SHA-256-bound batch
receipt and approval commits one total source debit plus one credit per item
inside a single SQLite transaction.

## Prerequisite

Start the existing local model supervisors first. Dimon only starts the web
gateway; it asks the existing Jarvis lifecycle supervisor to launch and stop
Nemotron as voice sessions require:

- endpoint: `http://127.0.0.1:8080/v1`
- model selected in `apps/dimon_live/.env`: `qwen35-9b-abliterated-q5`
- Nemotron realtime WebSocket: `ws://127.0.0.1:8004/v1/realtime`
- Kokoro HTTP: `http://127.0.0.1:3001`

Nemotron does not need to be permanently listening on `:8004`. The default
`DIMON_NEMOTRON_CONTROL_SOCKET` is
`/run/user/<uid>/jarvis-nemotron.ctl.sock`; the Jarvis supervisor must own that
socket. A voice turn acquires it, waits for `:8004`, and releases it afterward.
If that supervisor is absent or startup exceeds `DIMON_NEMOTRON_START_TIMEOUT`,
Dimon reports `NEMOTRON_UNAVAILABLE`; that is a speech-service issue, not a
transcript or ledger-policy decision.

`DIMON_QWEN_MODEL` remains configurable for controlled comparisons, but the
prototype's persistent default is Qwen3.5. Explicit shell or service
environment variables take precedence over `.env`:

```bash
DIMON_QWEN_MODEL=qwen35-9b-abliterated-q5 \
python3 -m uvicorn apps.dimon_live.app:app --host 127.0.0.1 --port 8890
```

Use `.env.example` as the configuration template. The active `.env` is ignored
by Git so machine-local choices and future credentials are not committed.

## Start

From the repository root:

```bash
python3 -m uvicorn apps.dimon_live.app:app --host 127.0.0.1 --port 8890
```

Open [http://127.0.0.1:8890](http://127.0.0.1:8890).

The runtime database defaults to `apps/dimon_live/data/demo.sqlite3`. Set
`DIMON_DB_PATH` to use another local path.

## Seed ledger

| Account | Starting balance |
| --- | ---: |
| savings | S$1,000.00 |
| John Smith | S$100.00 |
| Alicia Powell | S$75.00 |
| John Tan | S$75.00 |
| JXXX SXXXX (synthetic PayNow destination) | S$0.00 |

## Demo sequence

1. Missing amount — `send Alicia Powell money for flowers`, then reply
   `ten dollars`.
   Expected: the first turn stores `MISSING_AMOUNT` with savings and Alicia
   Powell locked. The reply creates an immutable S$10 receipt without calling
   Qwen again. Approval produces one S$10 debit and one balanced credit.
2. Grounded exact command — `send Alicia Powell $10` or
   `send $10 to Alicia Powell from savings`.
   Expected: the narrow affirmative command settles immediately. Adding a memo,
   condition, or question—such as `send Alicia Powell $10 for flowers`—requires
   immutable receipt confirmation. Python derives `$10` as exactly `1000`
   minor units from the original text; Qwen neither returns nor converts the
   amount.
3. Exact synthetic PayNow — `PayNow 8695 2933 ten dollars for flowers`.
   Expected: the directory returns `JXXX SXXXX`; a lookup-backed confirmation
   receipt is mandatory. Approval produces one balanced synthetic transfer.
4. Changed PayNow digit — `PayNow 8695 2938 ten dollars`.
   Expected: `PAYNOW_PROXY_NOT_FOUND`; no fuzzy digit correction or mutation.
5. Phonetic saved-name suggestion — `send Alesha $10`.
   Expected: `PHONETIC_RECIPIENT_MATCH` suggests Alicia Powell and requires
   confirmation. No balance changes before selection.
6. Ambiguous saved name — `Send John S$2.50`.
   Expected: receipts for John Smith and John Tan; selecting one supersedes the
   sibling.
7. Negated or unfunded request — `Do not send John Smith S$50 from savings.` or
   `Send John Smith S$2,000 from savings.`
   Expected: blocked with no mutation.
8. Select **Reset demo** and confirm. All synthetic balances, lookups,
   clarifications, receipts, and ledger history are restored.
9. Atomic batch — `Send John Smith S$6.50 and Alicia Powell S$4`.
   Expected: one review card lists both transfers and a S$10.50 total. Before
   approval no balance changes. One approval commits both routes; retry returns
   the same batch transaction. PayNow, shared amounts, fuzzy recipients, and
   more than four items are intentionally unsupported in a batch.

Every new browser intention gets a request UUID. If a response is interrupted,
the UI preserves and retries the identical UUID and text. At the API level,
replaying those coordinates returns the stored result without a second Qwen
call or second debit; reusing a UUID with different text is blocked. Pending
receipts expire after five minutes. Repeating a successful approval returns the
existing transaction, while selecting one ambiguous recipient supersedes the
unselected sibling receipts.

The confirmation request is deliberately narrow:

```http
POST /api/receipts/{receipt_id}/approve
Content-Type: application/json

{"receipt_digest":"<64 lowercase hexadecimal characters>"}
```

Amount, source, recipient, currency, identity, and authorization method are
loaded from the immutable server-owned receipt and cannot be supplied by the
browser.

Batch confirmation uses the same digest-only body:

```http
POST /api/batch-receipts/{receipt_id}/approve
Content-Type: application/json

{"receipt_digest":"<64 lowercase hexadecimal characters>"}
```

The amount-continuation request is independently narrow:

```http
POST /api/clarifications/{clarification_id}/respond
Content-Type: application/json

{"request_id":"<new UUID>","text":"ten dollars"}
```

That endpoint accepts no amount field, account ID, recipient, proxy, receipt
digest, or arbitrary command. A lost response is retried with the identical
clarification ID, response UUID, and text.

## Troubleshooting

- `MODEL_UNAVAILABLE`: confirm the Qwen endpoint is listening on port 8080 and
  that the configured model ID matches the loaded model.
- Slow first result: local inference is synchronous in this intentionally
  single-user prototype and may take several seconds.
- Interrupted approval response: the ledger outcome may already be committed.
  Use the preserved **Retry exact receipt** control; it resends the same receipt
  ID and digest and cannot create a second debit.
- Interrupted first response: use **Retry exact request**. It reuses the same
  request UUID and text, so a transfer that already settled cannot debit twice.
- Unexpected old balances: use **Reset demo**, or stop the server and point
  `DIMON_DB_PATH` at a fresh SQLite file.

The PayNow directory, proxy, masked name, accounts, lookup records, and ledger
are entirely synthetic. This application is not connected to PayNow, a bank,
broker, or any payment rail. Never
replace its synthetic executor with a real-money integration without a new
authorization, confirmation, authentication, audit, and threat model.
