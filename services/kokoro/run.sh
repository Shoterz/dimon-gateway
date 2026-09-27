#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "--check" ]]; then
  check_only=1
  shift
else
  check_only=0
fi
if [[ $# -ne 0 ]]; then
  echo "usage: services/kokoro/run.sh [--check]" >&2
  exit 2
fi

: "${DIMON_KOKORO_MODEL_PATH:?DIMON_KOKORO_MODEL_PATH is required}"
: "${DIMON_KOKORO_VOICE_PATH:?DIMON_KOKORO_VOICE_PATH is required}"
: "${DIMON_KOKORO_APP_DIR:?DIMON_KOKORO_APP_DIR is required}"
: "${DIMON_KOKORO_PYTHON:?DIMON_KOKORO_PYTHON is required}"
[[ -s "$DIMON_KOKORO_MODEL_PATH" ]] || { echo "KOKORO_ARTIFACT_NOT_FOUND" >&2; exit 1; }
[[ -s "$DIMON_KOKORO_VOICE_PATH" ]] || { echo "KOKORO_VOICE_NOT_FOUND" >&2; exit 1; }
[[ -f "$DIMON_KOKORO_APP_DIR/app.py" ]] || { echo "KOKORO_APP_NOT_FOUND" >&2; exit 1; }
[[ -x "$DIMON_KOKORO_PYTHON" ]] || { echo "KOKORO_RUNTIME_NOT_EXECUTABLE" >&2; exit 1; }

if (( check_only )); then
  echo "kokoro: READY"
  exit 0
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$DIMON_KOKORO_PYTHON" "$repo_root/services/kokoro/server.py" \
  --app-dir "$DIMON_KOKORO_APP_DIR" \
  --host 127.0.0.1 \
  --port 3001
