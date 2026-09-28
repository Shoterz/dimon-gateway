#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "--check" ]]; then
  check_only=1
  shift
else
  check_only=0
fi
if [[ $# -ne 0 ]]; then
  echo "usage: services/whisper_intel/run.sh [--check]" >&2
  exit 2
fi

: "${DIMON_WHISPER_MODEL_PATH:?DIMON_WHISPER_MODEL_PATH is required}"
: "${DIMON_WHISPER_PYTHON:?DIMON_WHISPER_PYTHON is required}"
device="${DIMON_WHISPER_DEVICE:-GPU}"
[[ -e "$DIMON_WHISPER_MODEL_PATH" ]] || { echo "WHISPER_ARTIFACT_NOT_FOUND" >&2; exit 1; }
[[ -x "$DIMON_WHISPER_PYTHON" ]] || { echo "WHISPER_RUNTIME_NOT_EXECUTABLE" >&2; exit 1; }

if (( check_only )); then
  echo "whisper_intel: READY"
  exit 0
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$DIMON_WHISPER_PYTHON" "$repo_root/services/whisper_intel/server.py" \
  --model "$DIMON_WHISPER_MODEL_PATH" \
  --device "$device" \
  --host 127.0.0.1 \
  --port 8005
