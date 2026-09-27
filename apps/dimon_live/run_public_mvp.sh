#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_ROOT"

export DIMON_PORT="${DIMON_PORT:-8890}"
export DIMON_OWNER_ORIGIN="${DIMON_OWNER_ORIGIN:-https://dimon.stocksuite.app}"
export DIMON_DB_OWNER_PATH="${DIMON_DB_OWNER_PATH:-$PROJECT_ROOT/runtime/ledgers/owner.sqlite3}"
export DIMON_VISITOR_DB_DIR="${DIMON_VISITOR_DB_DIR:-$PROJECT_ROOT/runtime/ledgers/visitors}"
export DIMON_AUDIO_DIR="${DIMON_AUDIO_DIR:-$PROJECT_ROOT/runtime/audio}"

echo "Dimon gateway: http://127.0.0.1:${DIMON_PORT}"
echo "Configured owner origin: ${DIMON_OWNER_ORIGIN}"
echo "Visitor origins: explicit process environment or apps/dimon_live/.env"
echo "Shared model services remain private on loopback."

exec python3 -m uvicorn apps.dimon_live.app:app \
  --host 127.0.0.1 \
  --port "$DIMON_PORT"
