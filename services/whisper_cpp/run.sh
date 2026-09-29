#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "--check" ]]; then
  check_only=1
  shift
else
  check_only=0
fi
if [[ $# -ne 0 ]]; then
  echo "usage: services/whisper_cpp/run.sh [--check]" >&2
  exit 2
fi

: "${DIMON_WHISPER_CPP_MODEL_PATH:?DIMON_WHISPER_CPP_MODEL_PATH is required}"
: "${DIMON_WHISPER_CPP_BINARY_PATH:?DIMON_WHISPER_CPP_BINARY_PATH is required}"
[[ -s "$DIMON_WHISPER_CPP_MODEL_PATH" ]] || { echo "WHISPER_ARTIFACT_NOT_FOUND" >&2; exit 1; }
[[ -x "$DIMON_WHISPER_CPP_BINARY_PATH" ]] || { echo "WHISPER_RUNTIME_NOT_EXECUTABLE" >&2; exit 1; }

if (( check_only )); then
  echo "whisper_cpp: READY"
  exit 0
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"
exec uv run python "$repo_root/services/whisper_cpp/server.py" \
  --binary "$DIMON_WHISPER_CPP_BINARY_PATH" \
  --model "$DIMON_WHISPER_CPP_MODEL_PATH" \
  --host 127.0.0.1 \
  --port 8006
