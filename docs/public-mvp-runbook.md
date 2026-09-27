# Dimon public MVP runbook

This is a synthetic banking demonstration. It never connects to a real bank or
PayNow account.

## Start

1. Keep the existing shared inference services running: Qwen on `127.0.0.1:8080`,
   Nemotron on `127.0.0.1:8004`, and Kokoro on `127.0.0.1:3001`.
2. Copy `apps/dimon_live/.env.example` to `apps/dimon_live/.env`.
3. Set `DIMON_ALLOWED_ORIGINS` to the stable owner origin and the current ngrok
   HTTPS origin. Free ngrok hostnames change, so restart after changing it.
4. If you want the stable hostname to use the persistent owner ledger, put it
   behind Cloudflare Access and set `DIMON_OWNER_ACCESS_AUDIENCE`. Otherwise it
   safely behaves as another visitor session and requires no Access setup.
5. Start `./apps/dimon_live/run_public_mvp.sh`.
6. Forward both Cloudflare Tunnel and ngrok to `http://127.0.0.1:8890`.

## What friends see

The first page load creates a visitor-scoped synthetic ledger. Their balances,
receipts, reset button, and Kokoro audio artifacts are isolated from every
other visitor. They can type or press **Speak request**. Audio streams to the
private Nemotron service while they talk and submits automatically after a
short conversational silence; clicking again remains a manual-send fallback.
Only one voice turn is allowed at a time; a second user receives `VOICE_BUSY`
and can type instead.

## Owner route

Owner mode is opt-in. When `DIMON_OWNER_ACCESS_AUDIENCE` is set, the stable
hostname must be behind Cloudflare Access. Dimon accepts the owner SQLite file
only when the trusted `Cf-Access-Jwt-Assertion` has a future `exp`, the
configured audience, and an identity claim. Missing or invalid access never
falls back to owner data. When the audience is blank, the hostname uses an
isolated visitor ledger instead.

## Demo script

- Submit a clear typed transfer and show the receipt/ledger change.
- Open the ngrok URL in a second browser and show a fresh seeded ledger.
- Start a voice turn in one browser and press Speak request in another to show
  the busy guard.
- Try an ambiguous or prompt-injected request and show no mutation.
- Approve a stored receipt and verify the balances update immediately; the
  short Kokoro result summary appears when background synthesis finishes.

## Stop and clean up

Stop only the Dimon process when the demo ends. Visitor database and audio files
expire after `DIMON_VISITOR_TTL_SECONDS`; never expose `runtime/ledgers/` through
the tunnel. The model services can remain managed by the existing GPU Overlord.
